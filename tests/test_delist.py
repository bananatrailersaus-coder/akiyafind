import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya", SECRET_KEY="testsecret")

import psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS saved_searches, listings, users, crawl_log CASCADE")
# Production-like table: no created_at/last_seen_at/delisted_at yet
cur.execute("""CREATE TABLE listings (id SERIAL PRIMARY KEY, title_en TEXT, title_jp TEXT, prefecture TEXT, city TEXT,
  price_jpy BIGINT, size_m2 FLOAT, source_name TEXT, source_url TEXT UNIQUE, is_free BOOLEAN, lat FLOAT, lng FLOAT, image_url TEXT)""")
B = "https://www.akiya-athome.jp/bukken/detail/"
for k in "ABC":
    cur.execute("INSERT INTO listings (title_jp, prefecture, city, price_jpy, source_url) VALUES (%s,'Nagano','松本市',1000000,%s)", (k, B+k))
cur.execute("INSERT INTO listings (title_jp, prefecture, price_jpy, source_url) VALUES ('K','Kyoto',500000,%s)", (B+"K",))
conn.commit()

import crawler_v3
crawler_v3.time.sleep = lambda s: None

def page_html(ids, price="50万円"):
    cards = "".join(f'<div class="card"><div><a href="/bukken/detail/{i}">{i}</a><p>長野県松本市</p><p>{price}</p><p>120㎡</p>'
                    f'<div class="imageCenter"><img src="//img.athome.jp/{i}.jpg"></div></div></div>' for i in ids)
    return f"<html><body>{cards}</body></html>"

class Resp:
    def __init__(self, status, text=""): self.status_code, self.text = status, text
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")

def serve(pages, fail_on=None):
    """pages: list of id-lists; after them an empty page. fail_on: page number that always errors."""
    def get(url, **kw):
        if url.endswith("/robots.txt"): return Resp(404)
        n = int(url.split("page=")[1])
        if fail_on == n: return Resp(503)
        return Resp(200, page_html(pages[n-1]) if n <= len(pages) else "<html><body>none</body></html>")
    crawler_v3.requests.get = get

def state():
    cur.execute("SELECT title_jp, delisted_at IS NOT NULL, price_jpy, is_free FROM listings WHERE prefecture='Nagano' ORDER BY title_jp")
    out = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    conn.commit()
    return out

# 1. Error mid-crawl (page 2 fails after retries): nothing delisted
serve([["A"], ["B"]], fail_on=2)
crawler_v3.main(["20"])
s = state(); print("after failed crawl:", s); assert not any(v[0] for v in s.values())

# 2. Complete crawl: A, B, D seen; C missing -> C delisted, D new, Kyoto untouched
serve([["A", "B"], ["D"]])
crawler_v3.main(["20"])
s = state(); print("after complete crawl:", s)
assert s["C"][0] is True and not s["A"][0] and not s["B"][0] and not s["D"][0]
assert s["A"][1] == 500000, "price should update on re-crawl"
cur.execute("SELECT delisted_at FROM listings WHERE prefecture='Kyoto'"); assert cur.fetchone()[0] is None; conn.commit()
cur.execute("SELECT complete, seen, delisted FROM crawl_log ORDER BY id"); log = cur.fetchall(); print("crawl_log:", log)
assert log == [(False, 1, 0), (True, 3, 1)]
conn.commit()

# 3. Broken-looking crawl (sees 1 of previous 3) -> nothing more delisted
serve([["A"]])
crawler_v3.main(["20"])
s = state(); assert not s["B"][0] and not s["D"][0]; print("suspicious crawl skipped delisting")

# 4. Zero listings on page 1 (layout change) -> nothing delisted
serve([])
crawler_v3.main(["20"])
assert not any(v[0] for k, v in state().items() if k != "C")

# 5. C comes back -> un-delisted; inserted-count only counts new rows
serve([["A", "B", "C", "D"]])
seen, inserted, delisted = crawler_v3.crawl_prefecture("20", "Nagano", conn)
print("relist:", seen, inserted, delisted); assert (seen, inserted, delisted) == (4, 0, 0)
assert not state()["C"][0]

# 6. Server and alerts hide delisted listings
serve([["A", "B"]])
crawler_v3.main(["20"])  # C, D delisted
from starlette.testclient import TestClient
import server
c = TestClient(server.app)
titles = lambda r: sorted(l["title"] for l in r.json()["listings"])
cur.execute("UPDATE listings SET title_en = title_jp"); conn.commit()
assert titles(c.get("/api/search", params={"prefecture": "Nagano"})) == ["A", "B"]
assert sorted(t for t in titles(c.get("/api/listings"))) == ["A", "B", "K"]
assert c.get("/api/counts").json()["counts"] == {"Nagano": 2, "Kyoto": 1}
print("API hides delisted: ok")
print("ALL PASSED")
