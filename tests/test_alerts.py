import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
import json, base64, re
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya", SECRET_KEY="testsecret",
                  RESEND_API_KEY="re_test", BASE_URL="https://akiyafind.com")

import psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS saved_searches, listings, users CASCADE")
# Production-like listings table WITHOUT created_at, to exercise the migration
cur.execute("""CREATE TABLE listings (id SERIAL PRIMARY KEY, title_en TEXT, title_jp TEXT, prefecture TEXT, city TEXT,
  price_jpy BIGINT, size_m2 FLOAT, source_name TEXT, source_url TEXT UNIQUE, is_free BOOLEAN, lat FLOAT, lng FLOAT, image_url TEXT)""")
cur.execute("INSERT INTO listings (title_en, prefecture, city, price_jpy, source_url) VALUES ('Old house','Nagano','Matsumoto',500000,'https://x/old')")
conn.commit()

from starlette.testclient import TestClient
from itsdangerous import TimestampSigner
import server, alerts
c = TestClient(server.app)

def login(email):
    u = server.upsert_user("g-"+email, email, "Tester", "")
    data = base64.b64encode(json.dumps({"user": u}).encode())
    c.cookies.clear()
    c.cookies.set("session", TimestampSigner("testsecret").sign(data).decode())
    return u

r = c.get("/api/alerts"); assert r.status_code == 401, r.status_code
r = c.post("/api/alerts", json={"prefecture": "Nagano"}); assert r.status_code == 401
u = login("a@example.com")
me = c.get("/api/me").json(); print("me:", me["payments_enabled"], me["alert_limit"])
assert me["payments_enabled"] is False and me["alert_limit"] == 1
r = c.post("/api/alerts", json={"prefecture": "Nagano", "max_price": 1000000}); print("create:", r.status_code, r.json())
assert r.status_code == 200
r = c.post("/api/alerts", json={"prefecture": "Nagano", "max_price": 1000000}); assert r.status_code == 409, r.status_code
r = c.post("/api/alerts", json={"prefecture": "Kyoto"}); assert r.status_code == 403, r.status_code; print("limit:", r.json())
r = c.post("/api/alerts", json={"min_price": "abc"}); assert r.status_code in (400, 403)
r = c.post("/api/checkout", json={"plan": "searcher"}); assert r.status_code == 503, r.status_code
# Upgrade via DB (as webhook would) -> /api/me must reflect it without re-login
cur.execute("UPDATE users SET tier='searcher' WHERE email='a@example.com'"); conn.commit()
assert c.get("/api/me").json()["alert_limit"] == 10
r = c.post("/api/alerts", json={"q": "kyoto"}); assert r.status_code == 200
alerts_list = c.get("/api/alerts").json(); print("list:", alerts_list); assert len(alerts_list["alerts"]) == 2

# Another user can't delete a's alert
login("b@example.com")
aid = alerts_list["alerts"][0]["id"]
assert c.delete(f"/api/alerts/{aid}").status_code == 404
login("a@example.com")

# Search API still works via shared filter
r = c.get("/api/search", params={"prefecture": "nagano", "max_price": 1000000}); assert len(r.json()["listings"]) == 1

# New listings arrive after the searches were saved
cur.execute("""INSERT INTO listings (title_en, prefecture, city, price_jpy, size_m2, source_url) VALUES
 ('Farmhouse <b>','Nagano','Ina',0,120.5,'https://x/1'),
 ('Townhouse','Nagano','Ueda',3000000,80,'https://x/2'),
 ('Machiya','Kyoto','Kyoto',900000,60,'https://x/3')""")
conn.commit()

sent = []
class R:
    status_code = 200
    def raise_for_status(self): pass
alerts.requests.post = lambda url, json=None, headers=None, timeout=None: (sent.append(json), R())[1]
alerts.time.sleep = lambda s: None
alerts.run()
for m in sent: print("EMAIL to", m["to"], "|", m["subject"], "|", m["headers"]["List-Unsubscribe"])
assert len(sent) == 2
nagano = [m for m in sent if "Nagano" in m["subject"]][0]
assert "Farmhouse &lt;b&gt;" in nagano["html"] and "Townhouse" not in nagano["html"] and "Old house" not in nagano["html"]
assert "Machiya" in [m for m in sent if "kyoto" in m["subject"]][0]["html"]

# Second run: nothing new -> no emails
sent.clear(); alerts.run(); assert sent == [], sent

# Failed send keeps watermark -> retried next run
cur.execute("INSERT INTO listings (title_en, prefecture, city, price_jpy, source_url) VALUES ('Cottage','Nagano','Iida',100000,'https://x/4')"); conn.commit()
class Bad(R):
    status_code = 500
    def raise_for_status(self): raise RuntimeError("boom")
alerts.requests.post = lambda *a, **k: Bad()
try: alerts.run()
except SystemExit as e: print("exit code on failure:", e.code)
alerts.requests.post = lambda url, json=None, headers=None, timeout=None: (sent.append(json), R())[1]
sent.clear(); alerts.run(); assert len(sent) == 1 and "Cottage" in sent[0]["html"]; print("retry ok")

# Unsubscribe: GET doesn't delete, POST does
token = re.search(r"token=([\w-]+)", sent[0]["headers"]["List-Unsubscribe"]).group(1)
c.get(f"/alerts/unsubscribe?token={token}")
assert len(c.get("/api/alerts").json()["alerts"]) == 2
r = c.post(f"/alerts/unsubscribe?token={token}", data={"List-Unsubscribe": "One-Click"}); assert r.status_code == 200
assert len(c.get("/api/alerts").json()["alerts"]) == 1
assert c.delete(f"/api/alerts/{c.get('/api/alerts').json()['alerts'][0]['id']}").status_code == 200
assert c.get("/api/alerts").json()["alerts"] == []

# Image proxy rejects non-allowed hosts
assert c.get("/api/img", params={"url": "http://169.254.169.254/"}).status_code == 400
print("ALL PASSED")
