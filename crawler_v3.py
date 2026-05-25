"""
crawler_v3.py — AkiyaFind
Enhanced version of the existing crawler.py.

Key fix: removes the broken JS next-link detection.
Instead, keeps incrementing ?page=N until a page returns 0 listings.
This works because /buy/{code}/?page=N serves plain HTML — no browser needed.

Usage:
    export DATABASE_URL="postgresql://..."
    python3 crawler_v3.py                        # all 47 prefectures
    python3 crawler_v3.py --prefectures 01 13 27  # specific prefectures
    python3 crawler_v3.py --start-from 10         # resume from index 10
"""

import requests
from bs4 import BeautifulSoup
import psycopg2
import os, re, time, argparse

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en;q=0.9',
}

PREFECTURES = {
    '01': 'Hokkaido',   '02': 'Aomori',    '03': 'Iwate',
    '04': 'Miyagi',     '05': 'Akita',     '06': 'Yamagata',
    '07': 'Fukushima',  '08': 'Ibaraki',   '09': 'Tochigi',
    '10': 'Gunma',      '11': 'Saitama',   '12': 'Chiba',
    '13': 'Tokyo',      '14': 'Kanagawa',  '15': 'Niigata',
    '16': 'Toyama',     '17': 'Ishikawa',  '18': 'Fukui',
    '19': 'Yamanashi',  '20': 'Nagano',    '21': 'Gifu',
    '22': 'Shizuoka',   '23': 'Aichi',     '24': 'Mie',
    '25': 'Shiga',      '26': 'Kyoto',     '27': 'Osaka',
    '28': 'Hyogo',      '29': 'Nara',      '30': 'Wakayama',
    '31': 'Tottori',    '32': 'Shimane',   '33': 'Okayama',
    '34': 'Hiroshima',  '35': 'Yamaguchi', '36': 'Tokushima',
    '37': 'Kagawa',     '38': 'Ehime',     '39': 'Kochi',
    '40': 'Fukuoka',    '41': 'Saga',      '42': 'Nagasaki',
    '43': 'Kumamoto',   '44': 'Oita',      '45': 'Miyazaki',
    '46': 'Kagoshima',  '47': 'Okinawa',
}

def get_db():
    return psycopg2.connect(DATABASE_URL)

def save_listing(cur, listing):
    cur.execute("""
        INSERT INTO listings
            (source_url, prefecture, city, title_jp, title_en,
             price_jpy, size_m2, source_name, is_free)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_url) DO NOTHING
    """, (
        listing['source_url'], listing['prefecture'], listing['city'],
        listing['title_jp'], listing['title_en'], listing['price_jpy'],
        listing['size_m2'], listing['source_name'], listing['price_jpy'] == 0
    ))
    return cur.rowcount == 1

def parse_price(text):
    if not text:
        return None
    if any(w in text for w in ['無料', '贈与', '譲渡']):
        return 0
    m = re.search(r'([\d,]+)\s*万円', text)
    if m:
        return int(m.group(1).replace(',', '')) * 10000
    m = re.search(r'([\d,]+)\s*円', text)
    if m:
        return int(m.group(1).replace(',', ''))
    return None

def parse_size(text):
    if not text:
        return None
    m = re.search(r'([\d.]+)\s*[㎡m²]', text)
    return float(m.group(1)) if m else None

def crawl_prefecture(code, pref_en, conn):
    cur = conn.cursor()
    total_inserted = 0
    total_seen = 0
    page = 1

    while True:
        url = f"https://www.akiya-athome.jp/buy/{code}/?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            if r.status_code == 404:
                print(f"  404 on page {page} — done")
                break
            r.raise_for_status()
        except Exception as e:
            print(f"  ERROR page {page}: {e}")
            break

        soup = BeautifulSoup(r.text, 'html.parser')

        # Find all listing anchors — try multiple patterns
        listings_found = 0
        seen_urls = set()

        # Pattern 1: links containing /bukken/detail/
        detail_links = soup.find_all('a', href=re.compile(r'/bukken/detail/'))

        # Pattern 2: links to subdomains (municipality sites)
        if not detail_links:
            detail_links = soup.find_all('a', href=re.compile(r'akiya-athome\.jp/bukken/'))

        for a in detail_links:
            href = a.get('href', '')
            if not href:
                continue

            # Build full URL
            if href.startswith('http'):
                src_url = href
            else:
                src_url = 'https://www.akiya-athome.jp' + href

            if src_url in seen_urls:
                continue
            seen_urls.add(src_url)

            # Walk up to card container for data
            card = a
            for _ in range(5):
                p = card.parent
                if not p or p.name in ('body', 'html', '[document]'):
                    break
                card = p

            card_text = card.get_text(separator='\n', strip=True)
            lines = [l for l in card_text.split('\n') if l.strip()]

            title_jp = lines[0] if lines else ''

            # City — try to extract from card text or URL
            city = ''
            city_match = re.search(r'[\u4e00-\u9fff]{2,8}[市町村区郡]', card_text)
            if city_match:
                city = city_match.group(0)

            # Price
            price_jpy = None
            for line in lines:
                if '万円' in line or '円' in line or '無料' in line or '贈与' in line:
                    price_jpy = parse_price(line)
                    break

            # Size
            size_m2 = None
            for line in lines:
                if '㎡' in line or 'm²' in line:
                    size_m2 = parse_size(line)
                    break

            listing = {
                'source_url':  src_url,
                'prefecture':  pref_en,
                'city':        city,
                'title_jp':    title_jp,
                'title_en':    '',
                'price_jpy':   price_jpy,
                'size_m2':     size_m2,
                'source_name': 'akiya-athome.jp',
            }

            total_seen += 1
            listings_found += 1
            if save_listing(cur, listing):
                total_inserted += 1

        conn.commit()
        print(f"  Page {page}: {listings_found} listings ({total_inserted} new so far)")

        # THE KEY FIX: stop when page returns 0 listings, not when next-link disappears
        if listings_found == 0:
            break

        page += 1
        time.sleep(1.5)

    cur.close()
    return total_seen, total_inserted

def main(pref_filter=None, start_from=0):
    conn = get_db()
    grand_total_seen = 0
    grand_total_inserted = 0

    pref_items = list(PREFECTURES.items())[start_from:]
    if pref_filter:
        pref_items = [(k, v) for k, v in PREFECTURES.items() if k in pref_filter]

    for code, pref_en in pref_items:
        print(f"\n{'='*50}")
        print(f"  {pref_en} (code={code})")
        print(f"{'='*50}")
        seen, inserted = crawl_prefecture(code, pref_en, conn)
        grand_total_seen += seen
        grand_total_inserted += inserted
        print(f"  → {seen} seen, {inserted} new | running total inserted: {grand_total_inserted}")

    conn.close()
    print(f"\n{'='*50}")
    print(f"DONE — seen: {grand_total_seen} | inserted: {grand_total_inserted}")
    print(f"{'='*50}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefectures', nargs='*', help='e.g. --prefectures 01 13 27')
    parser.add_argument('--start-from', type=int, default=0)
    args = parser.parse_args()
    main(args.prefectures, args.start_from)
