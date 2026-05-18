import requests
from bs4 import BeautifulSoup
import psycopg2
import os
import re
import time

DATABASE_URL = os.environ.get('DATABASE_URL')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en;q=0.9',
}

PREFECTURES = {
    '01': 'Hokkaido',
    '02': 'Aomori',
    '03': 'Iwate',
    '04': 'Miyagi',
    '05': 'Akita',
    '06': 'Yamagata',
    '07': 'Fukushima',
    '08': 'Ibaraki',
    '09': 'Tochigi',
    '10': 'Gunma',
    '11': 'Saitama',
    '12': 'Chiba',
    '13': 'Tokyo',
    '14': 'Kanagawa',
    '15': 'Niigata',
    '16': 'Toyama',
    '17': 'Ishikawa',
    '18': 'Fukui',
    '19': 'Yamanashi',
    '20': 'Nagano',
    '21': 'Gifu',
    '22': 'Shizuoka',
    '23': 'Aichi',
    '24': 'Mie',
    '25': 'Shiga',
    '26': 'Kyoto',
    '27': 'Osaka',
    '28': 'Hyogo',
    '29': 'Nara',
    '30': 'Wakayama',
    '31': 'Tottori',
    '32': 'Shimane',
    '33': 'Okayama',
    '34': 'Hiroshima',
    '35': 'Yamaguchi',
    '36': 'Tokushima',
    '37': 'Kagawa',
    '38': 'Ehime',
    '39': 'Kochi',
    '40': 'Fukuoka',
    '41': 'Saga',
    '42': 'Nagasaki',
    '43': 'Kumamoto',
    '44': 'Oita',
    '45': 'Miyazaki',
    '46': 'Kagoshima',
    '47': 'Okinawa',
}

def get_db():
    return psycopg2.connect(DATABASE_URL)

def save_listing(listing):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO listings (source_url, prefecture, city, title_jp, title_en, price_jpy, size_m2, source_name, is_free)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_url) DO NOTHING
        """, (
            listing['source_url'], listing['prefecture'], listing['city'],
            listing['title_jp'], listing['title_en'], listing['price_jpy'],
            listing['size_m2'], listing['source_name'], listing['price_jpy'] == 0
        ))
        conn.commit()
        print("  SAVED: " + listing['prefecture'] + " | yen=" + str(listing['price_jpy']) + " | m2=" + str(listing['size_m2']))
    except Exception as e:
        print("  DB ERROR: " + str(e))
        conn.rollback()
    finally:
        cur.close()
        conn.close()

def crawl_prefecture(code, pref_en):
    count = 0
    page = 1
    seen_urls = set()
    print("\nCrawling " + pref_en + "...")

    while True:
        url = "https://www.akiya-athome.jp/buy/" + code + "/?page=" + str(page)
        print("  Page " + str(page))
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, 'html.parser')

            # Each listing is a div.detailOuter
            blocks = soup.find_all('div', class_='detailOuter')
            print("  Blocks found: " + str(len(blocks)))

            found_on_page = 0
            for block in blocks:
                # Get link from the parent anchor (sits above detailOuter)
                parent = block.parent
                link = parent.find('a', href=True) if parent else None
                if not link:
                    link = block.find('a', href=True)

                href = link['href'] if link else ''
                if href.startswith('/'):
                    src_url = 'https://www.akiya-athome.jp' + href
                else:
                    src_url = href if href else url + '#' + str(count)

                if src_url in seen_urls:
                    continue
                seen_urls.add(src_url)

                # Extract all dt/dd pairs
                fields = {}
                for dt in block.find_all('dt'):
                    key = dt.get_text(strip=True)
                    dd = dt.find_next_sibling('dd')
                    if dd:
                        fields[key] = dd.get_text(strip=True)

                # Price: 価格 => "1,500万円" (span + text)
                price_jpy = 0
                price_dd = None
                price_dl = block.find('dl', class_='price')
                if price_dl:
                    price_dd = price_dl.find('dd')
                if price_dd:
                    span = price_dd.find('span')
                    num = span.get_text(strip=True) if span else ''
                    num = num.replace(',', '')
                    if num.isdigit():
                        price_jpy = int(num) * 10000

                # Building area: 建物面積
                size_m2 = 0.0
                val = fields.get('\u5efa\u7269\u9762\u7a4d', '')
                if val:
                    m = re.search(r'[\d]+\.[\d]+', val)
                    if m:
                        size_m2 = float(m.group(0))
                if size_m2 == 0.0:
                    val = fields.get('\u571f\u5730\u9762\u7a4d', '')
                    if val:
                        m = re.search(r'[\d]+\.[\d]+', val)
                        if m:
                            size_m2 = float(m.group(0))

                rooms = fields.get('\u9593\u53d6', '')
                location = fields.get('\u6240\u5728\u5730', pref_en)
                title_jp = location
                title_en = 'Vacant house - ' + pref_en
                if rooms:
                    title_en += ' ' + rooms
                if price_jpy == 0:
                    title_en = 'Free transfer - ' + pref_en

                listing = {
                    'source_url': src_url,
                    'prefecture': pref_en,
                    'city': location[:50],
                    'title_jp': title_jp[:200],
                    'title_en': title_en,
                    'price_jpy': price_jpy,
                    'size_m2': size_m2,
                    'source_name': 'akiya-athome.jp'
                }
                save_listing(listing)
                count += 1
                found_on_page += 1

            if found_on_page == 0:
                break

            # Check for next page
            next_link = None
            for a in soup.find_all('a', href=True):
                if 'page=' + str(page + 1) in a.get('href', ''):
                    next_link = a
                    break

            if not next_link:
                break

            page += 1
            time.sleep(2)

        except Exception as e:
            print("  ERROR: " + str(e))
            break

    print("  TOTAL for " + pref_en + ": " + str(count))
    return count

if __name__ == "__main__":
    print("AkiyaFind Crawler - akiya-athome.jp")
    print("=" * 40)
    total = 0
    for code, pref_en in PREFECTURES.items():
        total += crawl_prefecture(code, pref_en)
    print("\nDone! Grand total: " + str(total))
