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

from schema import ensure_listing_columns

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

# A complete crawl that sees fewer than this share of the previous complete
# crawl's listings is treated as a broken crawl (blocked, layout change) and
# marks nothing as delisted.
MIN_SEEN_RATIO = 0.5

def save_listing(cur, listing):
    # Returns True only for a brand-new listing (xmax = 0 means INSERT, not UPDATE).
    cur.execute("""
        INSERT INTO listings
            (source_url, prefecture, city, title_jp, title_en,
             price_jpy, size_m2, source_name, is_free, image_url, last_seen_at, delisted_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW(), NULL)
        ON CONFLICT (source_url) DO UPDATE SET
            image_url    = EXCLUDED.image_url,
            price_jpy    = COALESCE(EXCLUDED.price_jpy, listings.price_jpy),
            is_free      = COALESCE(COALESCE(EXCLUDED.price_jpy, listings.price_jpy) = 0, FALSE),
            last_seen_at = NOW(),
            delisted_at  = NULL
        RETURNING (xmax = 0)
    """, (
        listing['source_url'], listing['prefecture'], listing['city'],
        listing['title_jp'], listing['title_en'], listing['price_jpy'],
        listing['size_m2'], listing['source_name'], listing['price_jpy'] == 0,
        listing.get('image_url', '')
    ))
    return cur.fetchone()[0]

def fetch_page(url, attempts=3):
    """Returns the response, None for a 404 (past the last page), or raises after retries."""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(5 * attempt)

def mark_delisted(conn, pref_en, crawl_started, seen):
    """After a complete crawl, hide listings in this prefecture that it didn't find."""
    cur = conn.cursor()
    cur.execute("""SELECT seen FROM crawl_log WHERE prefecture=%s AND complete
                   ORDER BY finished_at DESC LIMIT 1""", (pref_en,))
    prev = cur.fetchone()
    if seen == 0 or (prev and seen < prev[0] * MIN_SEEN_RATIO):
        print(f"  WARNING: saw {seen} listings (previous complete crawl: {prev[0] if prev else 'none'}) "
              f"— looks broken, not marking anything delisted")
        cur.close()
        return 0, False
    cur.execute("""UPDATE listings SET delisted_at = NOW()
                   WHERE prefecture=%s AND delisted_at IS NULL AND last_seen_at < %s""",
                (pref_en, crawl_started))
    delisted = cur.rowcount
    conn.commit()
    cur.close()
    return delisted, True

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

SITE = 'https://www.akiya-athome.jp'

# Placeholder / lazy-load images that must never be shown as a listing's photo.
NOT_A_PHOTO = re.compile(
    r'(?<![a-z])(no[_-]?(image|img|photo)|noimg|spacer|dummy)|(?<![a-z])(blank|loading)\.(gif|png|svg)|(?<!\d)1x1(?!\d)',
    re.I)

def absolute_url(u):
    u = (u or '').strip()
    if not u or u.startswith('data:'):
        return ''
    if u.startswith('//'):
        return 'https:' + u
    if u.startswith('/'):
        return SITE + u
    return u

def listing_key(href):
    """Same listing regardless of query string or fragment."""
    return absolute_url(href).split('#')[0].split('?')[0]

def image_src(img):
    """The real photo URL of an <img>, preferring lazy-load attributes over the placeholder src."""
    for attr in ('data-src', 'data-original', 'data-lazy-src', 'data-lazy'):
        u = absolute_url(img.get(attr))
        if u:
            return u
    srcset = img.get('data-srcset') or img.get('srcset')
    if srcset:
        # Candidates are "url 320w, url 640w"; the last is conventionally the largest.
        u = absolute_url(srcset.split(',')[-1].strip().split(' ')[0])
        if u:
            return u
    return absolute_url(img.get('src'))

def pick_photo(card, link_re):
    # The source site's thumbnail container first, then any image linked to the listing.
    candidates = []
    for div in card.find_all('div', class_='imageCenter'):
        candidates += div.find_all('img')
    for a in card.find_all('a', href=link_re):
        candidates += a.find_all('img')
    for img in candidates:
        u = image_src(img)
        if u and not NOT_A_PHOTO.search(u):
            return u
    return ''

def parse_listings(html, pref_en):
    """Parse one results page into listing dicts (no DB access)."""
    soup = BeautifulSoup(html, 'html.parser')
    listings = []
    seen_urls = set()

    # Pattern 1: links containing /bukken/detail/
    link_re = re.compile(r'/bukken/detail/')
    detail_links = soup.find_all('a', href=link_re)

    # Pattern 2: links to subdomains (municipality sites)
    if not detail_links:
        link_re = re.compile(r'akiya-athome\.jp/bukken/')
        detail_links = soup.find_all('a', href=link_re)

    for a in detail_links:
        href = a.get('href', '')
        if not href:
            continue

        # Build full URL
        if href.startswith('http'):
            src_url = href
        else:
            src_url = 'https://www.akiya-athome.jp' + href

        # One row per listing even when its card links to it more than once
        # (e.g. photo link and "?tab=map" link); the first link's URL is stored.
        key = listing_key(href)
        if key in seen_urls:
            continue
        seen_urls.add(key)

        # Walk up to the largest container that holds only this listing. A fixed
        # number of levels can overshoot into the list itself, and then every
        # listing on the page picks up the first card's photo, title and price.
        card = a
        while True:
            p = card.parent
            if not p or p.name in ('body', 'html', '[document]'):
                break
            if any(listing_key(x.get('href')) != key for x in p.find_all('a', href=link_re)):
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

        image_url = pick_photo(card, link_re)

        listings.append({
            'source_url':  src_url,
            'prefecture':  pref_en,
            'city':        city,
            'title_jp':    title_jp,
            'title_en':    '',
            'price_jpy':   price_jpy,
            'size_m2':     size_m2,
            'source_name': 'akiya-athome.jp',
            'image_url':   image_url,
        })
    return listings

def crawl_prefecture(code, pref_en, conn):
    cur = conn.cursor()
    cur.execute("SELECT NOW()")
    crawl_started = cur.fetchone()[0]
    conn.commit()  # so later NOW() calls in save_listing are after crawl_started

    total_inserted = 0
    total_seen = 0
    complete = False
    page = 1

    while True:
        url = f"https://www.akiya-athome.jp/buy/{code}/?page={page}"
        try:
            r = fetch_page(url)
        except Exception as e:
            print(f"  ERROR page {page}: {e} — crawl incomplete, nothing marked delisted")
            break
        if r is None:
            print(f"  404 on page {page} — done")
            complete = True
            break

        listings = parse_listings(r.text, pref_en)
        listings_found = len(listings)
        for listing in listings:
            total_seen += 1
            if save_listing(cur, listing):
                total_inserted += 1

        conn.commit()
        print(f"  Page {page}: {listings_found} listings ({total_inserted} new so far)")

        # THE KEY FIX: stop when page returns 0 listings, not when next-link disappears
        if listings_found == 0:
            complete = True
            break

        page += 1
        time.sleep(1.5)

    delisted = 0
    if complete:
        delisted, complete = mark_delisted(conn, pref_en, crawl_started, total_seen)
    cur.execute("""INSERT INTO crawl_log (prefecture, started_at, complete, seen, delisted)
                   VALUES (%s, %s, %s, %s, %s)""",
                (pref_en, crawl_started, complete, total_seen, delisted))
    conn.commit()
    cur.close()
    return total_seen, total_inserted, delisted

def main(pref_filter=None, start_from=0):
    conn = get_db()
    cur = conn.cursor()
    ensure_listing_columns(cur)
    conn.commit()
    cur.close()
    grand_total_seen = 0
    grand_total_inserted = 0
    grand_total_delisted = 0

    pref_items = list(PREFECTURES.items())[start_from:]
    if pref_filter:
        pref_items = [(k, v) for k, v in PREFECTURES.items() if k in pref_filter]

    for code, pref_en in pref_items:
        print(f"\n{'='*50}")
        print(f"  {pref_en} (code={code})")
        print(f"{'='*50}")
        seen, inserted, delisted = crawl_prefecture(code, pref_en, conn)
        grand_total_seen += seen
        grand_total_inserted += inserted
        grand_total_delisted += delisted
        print(f"  → {seen} seen, {inserted} new, {delisted} delisted | running total inserted: {grand_total_inserted}")

    conn.close()
    print(f"\n{'='*50}")
    print(f"DONE — seen: {grand_total_seen} | inserted: {grand_total_inserted} | delisted: {grand_total_delisted}")
    print(f"{'='*50}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefectures', nargs='*', help='e.g. --prefectures 01 13 27')
    parser.add_argument('--start-from', type=int, default=0)
    args = parser.parse_args()
    main(args.prefectures, args.start_from)
