import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya")

import psycopg2, requests
conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS saved_searches, listings, users, crawl_log CASCADE")
cur.execute("""CREATE TABLE listings (id SERIAL PRIMARY KEY, title_en TEXT, title_jp TEXT, prefecture TEXT, city TEXT,
  price_jpy BIGINT, size_m2 FLOAT, source_name TEXT, source_url TEXT UNIQUE, is_free BOOLEAN, lat FLOAT, lng FLOAT, image_url TEXT)""")
for k in "AB":
    cur.execute("INSERT INTO listings (title_jp, prefecture, price_jpy, source_url) VALUES (%s,'Hokkaido',1,%s)", (k, "https://www.akiya-athome.jp/bukken/detail/" + k))
conn.commit()
import crawler_v3 as c

sleeps, calls = [], []
c.time.sleep = lambda s: sleeps.append(s)
class Resp:
    def __init__(self, status, text=""): self.status_code, self.text = status, text
    def raise_for_status(self):
        if self.status_code >= 400: raise requests.HTTPError(f"HTTP {self.status_code}")
PAGE = lambda ids: "<html><body><ul>" + "".join(f'<li><a href="/bukken/detail/{i}">{i}</a><p>{i}</p><p>100万円</p></li>' for i in ids) + "</ul></body></html>"

def run(handler, prefs):
    sleeps.clear(); calls.clear()
    def get(url, **kw):
        calls.append(url); r = handler(url, len(calls))
        if isinstance(r, Exception): raise r
        return r
    c.requests.get = get
    try:
        c.main(prefs); return 0
    except SystemExit as e:
        return e.code

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); cond or fails.append(msg)
pages = lambda u: [x for x in calls if "page=" in x]

# 1. 403 on the first listing page: whole run stops after ONE page request, nothing delisted
code = run(lambda u, n: Resp(404) if u.endswith("robots.txt") else Resp(403), ["01", "02", "13"])
check(code == 2, "403 -> exit code 2 (run shows as failed in GitHub)")
check(len(pages(calls)) == 1, f"403 -> only 1 page request, no retries, no other prefectures (made {len(pages(calls))})")
cur.execute("SELECT COUNT(*) FROM listings WHERE delisted_at IS NOT NULL"); check(cur.fetchone()[0] == 0, "403 -> nothing delisted")
cur.execute("SELECT prefecture, complete FROM crawl_log"); check(cur.fetchall() == [("Hokkaido", False)], "403 -> logged as incomplete crawl")
conn.commit()

# 2. robots.txt itself 403 -> stop before any listing page
code = run(lambda u, n: Resp(403), ["01"])
check(code == 2 and pages(calls) == [], "robots.txt 403 -> stop with zero page requests")

# 3. robots.txt disallows listing pages for us
code = run(lambda u, n: Resp(200, "User-agent: *\nDisallow: /buy/\n") if u.endswith("robots.txt") else Resp(200, PAGE([])), ["01"])
check(code == 2 and pages(calls) == [], "robots.txt Disallow: /buy/ -> stop with zero page requests")

# 4. robots.txt allows, with a Crawl-delay we must honour
def h4(u, n):
    if u.endswith("robots.txt"): return Resp(200, "User-agent: *\nCrawl-delay: 5\nDisallow: /admin/\n")
    return Resp(200, PAGE(["A", "B"]) if "page=1" in u else PAGE([]))
code = run(h4, ["01"])
check(code == 0 and 5 in sleeps and 1.5 not in sleeps, f"Crawl-delay: 5 honoured between pages (sleeps={sleeps})")

# 5. 429 three times -> Blocked, with backoff in between
code = run(lambda u, n: Resp(404) if u.endswith("robots.txt") else Resp(429), ["01", "02"])
check(code == 2 and len(pages(calls)) == 3 and sleeps == [5, 10], f"429 x3 -> backoff 5s,10s then stop (calls={len(pages(calls))}, sleeps={sleeps})")

# 6. transient 500 then success -> retried, crawl completes
def h6(u, n):
    if u.endswith("robots.txt"): return Resp(404)
    if n == 2: return Resp(500)
    return Resp(200, PAGE(["A", "B"]) if "page=1" in u else PAGE([]))
code = run(h6, ["01"])
check(code == 0 and len(pages(calls)) == 3, "500 once -> retried and crawl completed")

# 7. connection error then success -> retried
def h7(u, n):
    if u.endswith("robots.txt"): return Resp(404)
    if n == 2: return requests.ConnectionError("reset")
    return Resp(200, PAGE(["A", "B"]) if "page=1" in u else PAGE([]))
code = run(h7, ["01"])
check(code == 0, "connection error once -> retried and crawl completed")

# 8. other 4xx (400) -> no retry, that prefecture incomplete, run carries on to the next
def h8(u, n):
    if u.endswith("robots.txt"): return Resp(404)
    if "/buy/01/" in u: return Resp(400)
    return Resp(200, PAGE([]))
code = run(h8, ["01", "02"])
check(code == 0 and sum("/buy/01/" in x for x in calls) == 1 and any("/buy/02/" in x for x in calls), "400 -> not retried, next prefecture still crawled")

# 9. first prefecture completes, second is blocked -> first's results kept, run stops
def h9(u, n):
    if u.endswith("robots.txt"): return Resp(404)
    if "/buy/02/" in u: return Resp(403)
    return Resp(200, PAGE(["A", "B"]) if "page=1" in u else PAGE([]))
code = run(h9, ["01", "02", "03"])
check(code == 2 and not any("/buy/03/" in x for x in calls), "blocked mid-run -> stops before the next prefecture")
cur.execute("SELECT prefecture, complete FROM crawl_log ORDER BY id DESC LIMIT 2"); rows = cur.fetchall(); conn.commit()
check(rows == [("Aomori", False), ("Hokkaido", True)], f"earlier complete prefecture kept as complete ({rows})")

# 10. requests identify the bot honestly
check(c.HEADERS["User-Agent"].startswith("AkiyaFindBot/"), "User-Agent identifies AkiyaFindBot")

print("FAILURES:", fails or "none"); sys.exit(1 if fails else 0)
