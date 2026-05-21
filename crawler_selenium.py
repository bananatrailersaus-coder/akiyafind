"""
crawler_selenium.py — AkiyaFind
Selenium + Chrome headless crawler for akiya-athome.jp
Handles JS-rendered pagination that plain HTTP cannot see.

Designed to run on GitHub Actions (Ubuntu) where Chrome is pre-installed.

Usage:
    export DATABASE_URL="postgresql://..."
    python3 crawler_selenium.py
    python3 crawler_selenium.py --prefectures 13 27 40
    python3 crawler_selenium.py --start-from 10
"""

import os, re, time, argparse, psycopg2
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

BASE_URL   = "https://www.akiya-athome.jp"
SEARCH_URL = f"{BASE_URL}/akiya/"
PAGE_WAIT  = 3       # seconds to wait after page load for JS to render
MAX_PAGES  = 100     # safety cap per city

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set.")

# ---------------------------------------------------------------------------
# All 47 prefectures
# ---------------------------------------------------------------------------
PREFECTURES = [
    (1,  "Hokkaido"),   (2,  "Aomori"),    (3,  "Iwate"),
    (4,  "Miyagi"),     (5,  "Akita"),     (6,  "Yamagata"),
    (7,  "Fukushima"),  (8,  "Ibaraki"),   (9,  "Tochigi"),
    (10, "Gunma"),      (11, "Saitama"),   (12, "Chiba"),
    (13, "Tokyo"),      (14, "Kanagawa"),  (15, "Niigata"),
    (16, "Toyama"),     (17, "Ishikawa"),  (18, "Fukui"),
    (19, "Yamanashi"),  (20, "Nagano"),    (21, "Gifu"),
    (22, "Shizuoka"),   (23, "Aichi"),     (24, "Mie"),
    (25, "Shiga"),      (26, "Kyoto"),     (27, "Osaka"),
    (28, "Hyogo"),      (29, "Nara"),      (30, "Wakayama"),
    (31, "Tottori"),    (32, "Shimane"),   (33, "Okayama"),
    (34, "Hiroshima"),  (35, "Yamaguchi"), (36, "Tokushima"),
    (37, "Kagawa"),     (38, "Ehime"),     (39, "Kochi"),
    (40, "Fukuoka"),    (41, "Saga"),      (42, "Nagasaki"),
    (43, "Kumamoto"),   (44, "Oita"),      (45, "Miyazaki"),
    (46, "Kagoshima"),  (47, "Okinawa"),
]

# ---------------------------------------------------------------------------
# Selenium driver setup
# ---------------------------------------------------------------------------
def make_driver():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # GitHub Actions has chromedriver on PATH; locally override with env var
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(driver_path) if driver_path else Service()
    return webdriver.Chrome(service=service, options=opts)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id          SERIAL PRIMARY KEY,
                title_en    TEXT,
                title_jp    TEXT,
                prefecture  TEXT,
                city        TEXT,
                price_jpy   BIGINT,
                size_m2     FLOAT,
                source_name TEXT,
                source_url  TEXT UNIQUE,
                is_free     BOOLEAN DEFAULT FALSE,
                lat         FLOAT,
                lng         FLOAT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()

def upsert(conn, listing):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO listings
                (title_en, title_jp, prefecture, city, price_jpy, size_m2,
                 source_name, source_url, is_free)
            VALUES
                (%(title_en)s, %(title_jp)s, %(prefecture)s, %(city)s,
                 %(price_jpy)s, %(size_m2)s, %(source_name)s, %(source_url)s,
                 %(is_free)s)
            ON CONFLICT (source_url) DO NOTHING
        """, listing)
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_price(text):
    if not text:
        return None, False
    if any(w in text for w in ["無料", "贈与", "譲渡"]):
        return 0, True
    m = re.search(r"([\d,]+)\s*万円", text)
    if m:
        return int(m.group(1).replace(",", "")) * 10_000, False
    m = re.search(r"([\d,]+)\s*円", text)
    if m:
        yen = int(m.group(1).replace(",", ""))
        return yen, yen == 0
    return None, False

def parse_size(text):
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*[㎡m²]", text)
    return float(m.group(1)) if m else None

# ---------------------------------------------------------------------------
# Scrape one loaded page — discovers selectors dynamically
# ---------------------------------------------------------------------------
def scrape_page(driver, prefecture, city):
    listings = []
    time.sleep(PAGE_WAIT)

    # Find all detail links — the most reliable anchor
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/akiya/detail']")
    except Exception:
        return []

    seen_urls = set()
    for link in links:
        try:
            href = link.get_attribute("href") or ""
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            # Walk up to find the card container (parent/grandparent of the link)
            card = link
            for _ in range(4):
                parent = card.find_element(By.XPATH, "..")
                # Stop if we've gone too far up (body/html level)
                if parent.tag_name.lower() in ("body", "html", "main", "section"):
                    break
                card = parent

            card_text = card.text

            # Extract title — first non-empty line usually
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            title_jp = lines[0] if lines else ""

            # Price — find 万円 or 無料
            price_jpy, is_free = None, False
            for line in lines:
                if "万円" in line or "無料" in line or "円" in line or "贈与" in line:
                    price_jpy, is_free = parse_price(line)
                    break

            # Size — find ㎡
            size_m2 = None
            for line in lines:
                if "㎡" in line or "m²" in line or "m2" in line:
                    size_m2 = parse_size(line)
                    break

            source_url = href if href.startswith("http") else BASE_URL + href

            listings.append({
                "title_jp":    title_jp,
                "title_en":    "",
                "prefecture":  prefecture,
                "city":        city,
                "price_jpy":   price_jpy,
                "is_free":     is_free,
                "size_m2":     size_m2,
                "source_url":  source_url,
                "source_name": "akiya-athome.jp",
            })
        except Exception as e:
            continue

    return listings

# ---------------------------------------------------------------------------
# Click to next page — tries multiple selector strategies
# ---------------------------------------------------------------------------
def go_next_page(driver):
    """Returns True if successfully navigated to next page, False if no more pages."""
    strategies = [
        # Text-based
        "a:contains('次へ')",           # jQuery-style (won't work natively)
        # CSS next-page patterns
        "a.next",
        "li.next > a",
        ".pagination .next a",
        ".pager .next a",
        "[class*='pager'] a[rel='next']",
        "a[rel='next']",
    ]

    # Try CSS selectors
    css_selectors = [
        "a.next", "li.next > a", ".pagination .next a",
        ".pager .next a", "a[rel='next']", ".pager-next a",
        "[class*='pagination'] li:last-child a",
    ]
    for sel in css_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn and btn.is_displayed():
                classes = btn.get_attribute("class") or ""
                if "disabled" in classes.lower():
                    return False
                href = btn.get_attribute("href")
                if href:
                    driver.get(href)
                else:
                    btn.click()
                return True
        except NoSuchElementException:
            continue

    # Try finding by Japanese text '次へ' or '次のページ'
    try:
        btns = driver.find_elements(By.TAG_NAME, "a")
        for btn in btns:
            try:
                text = btn.text.strip()
                if text in ("次へ", "次のページ", "次", ">", "›", "»"):
                    classes = btn.get_attribute("class") or ""
                    if "disabled" in classes.lower():
                        return False
                    href = btn.get_attribute("href")
                    if href and href != "#":
                        driver.get(href)
                        return True
                    btn.click()
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False  # No next page found

# ---------------------------------------------------------------------------
# Get city list for a prefecture
# ---------------------------------------------------------------------------
def get_cities(driver, ken_cd):
    url = f"{SEARCH_URL}?ken_cd[]={ken_cd}"
    driver.get(url)
    time.sleep(PAGE_WAIT)

    cities = []

    # Try checkboxes: input[name='gyosei_cd[]']
    inputs = driver.find_elements(By.CSS_SELECTOR, "input[name='gyosei_cd[]']")
    for inp in inputs:
        value = inp.get_attribute("value") or ""
        if not value:
            continue
        # Find label
        inp_id = inp.get_attribute("id") or ""
        label_text = value
        if inp_id:
            try:
                label = driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']")
                label_text = label.text.strip() or value
            except NoSuchElementException:
                pass
        cities.append((label_text, value))

    if cities:
        return cities

    # Try select options
    options = driver.find_elements(By.CSS_SELECTOR, "select[name='gyosei_cd[]'] option")
    for opt in options:
        value = opt.get_attribute("value") or ""
        text  = opt.text.strip()
        if value and value != "0" and value != "":
            cities.append((text or value, value))

    return cities

# ---------------------------------------------------------------------------
# Crawl one URL through all its pages
# ---------------------------------------------------------------------------
def crawl_url(driver, url, prefecture, city, conn):
    inserted_count = 0
    seen_count = 0
    driver.get(url)

    for page_num in range(1, MAX_PAGES + 1):
        listings = scrape_page(driver, prefecture, city)
        if not listings and page_num == 1:
            print(f"    No listings found on first page — skipping")
            break

        for listing in listings:
            seen_count += 1
            if upsert(conn, listing):
                inserted_count += 1

        print(f"    Page {page_num}: {len(listings)} listings scraped")

        if not go_next_page(driver):
            break

    return seen_count, inserted_count

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(prefectures_filter=None, start_from=0):
    conn = get_conn()
    ensure_table(conn)

    total_seen = 0
    total_inserted = 0

    to_crawl = PREFECTURES[start_from:]
    if prefectures_filter:
        to_crawl = [p for p in PREFECTURES if p[0] in prefectures_filter]

    driver = make_driver()
    print(f"Chrome started OK")

    try:
        for ken_cd, pref_en in to_crawl:
            print(f"\n{'='*55}")
            print(f"  {pref_en} (ken_cd={ken_cd})")
            print(f"{'='*55}")

            cities = get_cities(driver, ken_cd)

            if cities:
                print(f"  {len(cities)} cities found")
                for city_name, gyosei_cd in cities:
                    print(f"\n  → {city_name}")
                    url = f"{SEARCH_URL}?ken_cd[]={ken_cd}&gyosei_cd[]={gyosei_cd}"
                    seen, ins = crawl_url(driver, url, pref_en, city_name, conn)
                    total_seen += seen
                    total_inserted += ins
                    print(f"    {seen} seen, {ins} new | running total: {total_inserted}")
                    time.sleep(0.5)
            else:
                print(f"  No city list — crawling whole prefecture")
                url = f"{SEARCH_URL}?ken_cd[]={ken_cd}"
                seen, ins = crawl_url(driver, url, pref_en, "", conn)
                total_seen += seen
                total_inserted += ins
                print(f"  {seen} seen, {ins} new | running total: {total_inserted}")

    finally:
        driver.quit()
        conn.close()

    print(f"\n{'='*55}")
    print(f"DONE  —  seen: {total_seen}  |  newly inserted: {total_inserted}")
    print(f"{'='*55}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefectures", nargs="*", type=int)
    parser.add_argument("--start-from", type=int, default=0)
    args = parser.parse_args()
    main(args.prefectures, args.start_from)
