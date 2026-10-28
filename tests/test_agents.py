import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
import io, json, base64, re
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya", SECRET_KEY="testsecret",
                  ADMIN_EMAILS="admin@example.com", CONTACT_EMAIL="hello@akiyafind.com", BASE_URL="https://akiyafind.com")

import psycopg2
from PIL import Image
conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS enquiries, agent_photos, agent_listings, agent_profiles, saved_searches, listings, users, crawl_log CASCADE")
# Production-like listings table without the newer columns
cur.execute("""CREATE TABLE listings (id SERIAL PRIMARY KEY, title_en TEXT, title_jp TEXT, prefecture TEXT, city TEXT,
  price_jpy BIGINT, size_m2 FLOAT, source_name TEXT, source_url TEXT UNIQUE, is_free BOOLEAN, lat FLOAT, lng FLOAT, image_url TEXT)""")
cur.execute("INSERT INTO listings (title_en, prefecture, price_jpy, source_name, source_url, image_url) VALUES ('Crawled house','Nagano',100,'akiya-athome.jp','https://www.akiya-athome.jp/bukken/detail/C1','')")
conn.commit()

from starlette.testclient import TestClient
from itsdangerous import TimestampSigner
import server, agents, alerts, crawler_v3

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); cond or fails.append(msg)

def client_for(email):
    c = TestClient(server.app)
    if email:
        u = server.upsert_user("g-" + email, email, email.split("@")[0], "")
        c.cookies.set("session", TimestampSigner("testsecret").sign(base64.b64encode(json.dumps({"user": u}).encode())).decode())
    return c

def jpeg_with_gps(w=2400, h=1800):
    img = Image.new("RGB", (w, h), (200, 120, 40))
    exif = Image.Exif(); exif[0x0112] = 6  # rotate-90 orientation
    gps = exif.get_ifd(0x8825); gps[1] = "N"; gps[2] = (35.0, 41.0, 0.0); gps[3] = "E"; gps[4] = (139.0, 41.0, 0.0)  # GPS position
    b = io.BytesIO(); img.save(b, "JPEG", exif=exif.tobytes()); return b.getvalue()
def png(w=800, h=600):
    b = io.BytesIO(); Image.new("RGB", (w, h), (40, 90, 160)).save(b, "PNG"); return b.getvalue()

anon, agentA, agentB, admin = client_for(None), client_for("a@agency.jp"), client_for("b@other.jp"), client_for("admin@example.com")
LISTING = {"title": "Farmhouse with mountain views", "prefecture": "Nagano", "city": "Matsumoto", "price_jpy": "3,500,000",
           "land_m2": "420.5", "building_m2": "160", "year_built": "1978", "layout": "6DK", "description": "Line one\nLine two",
           "listing_url": "https://agency.jp/p/1"}
def post_listing(c, data=LISTING, photos=None):
    files = [("photos", (n, b, t)) for n, b, t in (photos if photos is not None else [("a.jpg", jpeg_with_gps(), "image/jpeg"), ("b.png", png(), "image/png")])]
    return c.post("/api/agent/listings", data=data, files=files)

# --- schema and pages
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='listings' AND column_name='agent_listing_id'"); check(cur.fetchone() is not None, "listings.agent_listing_id column added on startup")
conn.commit()
r = anon.get("/privacy"); check(r.status_code == 200 and "hello@akiyafind.com" in r.text and "af-footer" in r.text, "privacy page renders with contact email and footer")
check('data-domains="akiyafind.com,www.akiyafind.com"' in r.text and "02f86e1e-9e5a-4ebb-ad59-e9c03f227009" in r.text, "Umami tag injected with your website ID, limited to akiyafind.com")
check(anon.get("/terms").status_code == 200 and "{{CONTACT_EMAIL}}" not in anon.get("/terms").text, "terms page renders, placeholder replaced")
check("/agents" in anon.get("/search").text and "/privacy" in anon.get("/").text, "footer links on search and home pages")
check(anon.get("/agents").status_code == 200, "agents page renders")
check(anon.get("/admin").status_code == 404 and agentA.get("/admin").status_code == 404 and admin.get("/admin").status_code == 200, "admin page: 404 for anonymous and agents, 200 for admin")

# --- profile and listing submission
check(anon.get("/api/agent/me").json()["user"] is None, "anonymous /api/agent/me -> no user")
check(anon.put("/api/agent/profile", json={}).status_code == 401, "anonymous can't save a profile")
check(post_listing(agentA).status_code == 400, "can't submit a listing before saving company details")
check(agentA.put("/api/agent/profile", json={"company": "Alps Realty", "email": "a@agency.jp"}).status_code == 400, "profile without licence number rejected")
check(agentA.put("/api/agent/profile", json={"company": "Alps Realty", "email": "not-an-email", "license_no": "x"}).status_code == 400, "bad email rejected")
r = agentA.put("/api/agent/profile", json={"company": "Alps Realty", "contact_name": "Sato", "email": "a@agency.jp",
               "phone": "+81 263 00 0000", "website": "https://agency.jp", "license_no": "長野県知事(3)第1234号", "languages": "Japanese, English"})
check(r.status_code == 200, "valid profile saved")
check(post_listing(agentA, photos=[]).status_code == 400, "listing with no photos rejected")
check(post_listing(agentA, photos=[("x.jpg", b"not an image", "image/jpeg")]).status_code == 400, "non-image upload rejected")
check(post_listing(agentA, photos=[(f"{i}.png", png(50, 50), "image/png") for i in range(11)]).status_code == 400, "more than 10 photos rejected")
check(post_listing(agentA, data={**LISTING, "prefecture": "Narnia"}).status_code == 400, "unknown prefecture rejected")
check(post_listing(agentA, data={**LISTING, "price_jpy": "cheap"}).status_code == 400, "non-numeric price rejected")
r = post_listing(agentA); check(r.status_code == 200 and r.json()["status"] == "pending", "listing submitted -> pending")
lid = r.json()["id"]

cur.execute("SELECT id, data, width, height FROM agent_photos WHERE listing_id=%s ORDER BY position", (lid,)); photos = cur.fetchall(); conn.commit()
first = Image.open(io.BytesIO(bytes(photos[0][1])))
check(len(photos) == 2 and first.format == "JPEG" and max(first.size) == 1600, f"photos stored as JPEG, resized to 1600px max ({first.size})")
check(first.size == (1200, 1600), "orientation applied (landscape 2400x1800 tagged rotate-90 -> portrait)")
check(len(first.getexif()) == 0 and "exif" not in first.info, "EXIF/GPS metadata stripped")
p1, p2 = photos[0][0], photos[1][0]

# --- pending: private
srch = lambda c: c.get("/api/search", params={"prefecture": "Nagano"}).json()["listings"]
check(all("Farmhouse" not in l["title"] for l in srch(anon)), "pending listing not in search")
check(anon.get(f"/api/listing/{lid}").status_code == 404 and agentB.get(f"/api/listing/{lid}").status_code == 404, "pending listing hidden from public and other agents")
check(agentA.get(f"/api/listing/{lid}").status_code == 200, "owner can preview pending listing")
check(anon.get(f"/agent-photos/{p1}").status_code == 404 and agentA.get(f"/agent-photos/{p1}").status_code == 200, "pending photos visible to owner only")
check(agentA.post(f"/api/admin/listings/{lid}/approve").status_code == 404, "agent can't approve their own listing")

# --- approve
r = admin.post(f"/api/admin/listings/{lid}/approve"); check(r.status_code == 200, "admin approves")
hit = [l for l in srch(anon) if l["title"] == "Farmhouse with mountain views"]
check(len(hit) == 1 and hit[0]["image_url"] == f"/agent-photos/{p1}" and hit[0]["source_url"] == f"https://akiyafind.com/listing/{lid}"
      and hit[0]["source_name"] == "Alps Realty" and hit[0]["price_jpy"] == 3500000 and hit[0]["size_m2"] == 160, "approved listing appears in search with its photo, page and company")
pub = anon.get(f"/api/listing/{lid}").json()
check(pub["agent"]["license_no"] == "長野県知事(3)第1234号" and "review_note" not in pub and "email" not in pub["agent"], "public listing shows licence, hides agent email and review note")
r = anon.get(f"/agent-photos/{p2}"); check(r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "public" in r.headers["cache-control"], "approved photos public and cacheable")

# --- crawler must not delist agent listings
from datetime import datetime, timezone
delisted, ok = crawler_v3.mark_delisted(conn, "Nagano", datetime.now(timezone.utc), 1)
cur.execute("SELECT delisted_at IS NULL FROM listings WHERE agent_listing_id=%s", (lid,)); agent_active = cur.fetchone()[0]
cur.execute("SELECT delisted_at IS NOT NULL FROM listings WHERE source_url LIKE '%%/C1'"); crawled_gone = cur.fetchone()[0]; conn.commit()
check(agent_active and crawled_gone, "complete crawl delists the unseen crawled listing but never the agent listing")

# --- freshness ignores agent listings
f = anon.get("/api/listings").json()["freshness"]; check(f["refreshed_at"] == "2026-05-26", f"freshness still based on crawled data ({f})")

# --- enquiries
sent = []
class R: status_code = 200
agents.requests.post = lambda url, **kw: (sent.append(kw["json"]), R())[1]
agents.RESEND_API_KEY = "re_test"
agents._enquiry_times.clear()
r = anon.post(f"/api/listing/{lid}/enquire", json={"name": "Jane Buyer", "email": "jane@example.com", "message": "Still available?"})
check(r.status_code == 200 and len(sent) == 1 and sent[0]["to"] == ["a@agency.jp"] and sent[0]["reply_to"] == "jane@example.com"
      and "Still available?" in sent[0]["text"] and f"/listing/{lid}" in sent[0]["text"], "enquiry emailed to the agent with Reply-To the buyer")
cur.execute("SELECT name, emailed FROM enquiries"); check(cur.fetchall() == [("Jane Buyer", True)], "enquiry stored and marked emailed"); conn.commit()
check(anon.post(f"/api/listing/{lid}/enquire", json={"name": "bot", "email": "b@x.com", "message": "spam", "website": "http://spam"}).status_code == 200, "honeypot returns 'sent'...")
cur.execute("SELECT COUNT(*) FROM enquiries"); check(cur.fetchone()[0] == 1, "...but stores and sends nothing"); conn.commit()
check(anon.post(f"/api/listing/{lid}/enquire", json={"name": "x", "email": "nope", "message": "hi"}).status_code == 400, "enquiry with bad email rejected")
codes = [anon.post(f"/api/listing/{lid}/enquire", json={"name": "x", "email": "x@x.com", "message": "hi"}).status_code for _ in range(5)]
check(codes[:4] == [200] * 4 and codes[4] == 429, f"rate limit: 5 per hour per IP ({codes})")
me = agentA.get("/api/agent/me").json(); check(len(me["enquiries"]) == 5 and me["listings"][0]["status"] == "approved", "agent dashboard shows enquiries and live status")
check(len(admin.get("/api/admin/listings?status=approved").json()["enquiries"]) == 5, "admin sees enquiries")

# --- saved-search alerts include the agent listing
uid = server.upsert_user("g-buyer", "buyer@example.com", "Buyer", "")["id"]
cur.execute("INSERT INTO saved_searches (user_id, prefecture, unsub_token, last_checked_at) VALUES (%s,'Nagano','tok', NOW() - interval '1 day')", (uid,)); conn.commit()
mails = []
alerts.RESEND_API_KEY = "re_test"
alerts.requests.post = lambda url, json=None, **kw: (mails.append(json), R())[1]
R.raise_for_status = lambda self: None
alerts.time.sleep = lambda s: None
alerts.run()
check(len(mails) == 1 and "Farmhouse with mountain views" in mails[0]["html"] and f"https://akiyafind.com/listing/{lid}" in mails[0]["html"], "alert email includes the new agent listing with an absolute link")

# --- reject, withdraw
r2 = post_listing(agentA, data={**LISTING, "title": "Second house"}); lid2 = r2.json()["id"]
check(admin.post(f"/api/admin/listings/{lid2}/reject", json={"note": "Please add a clearer cover photo"}).status_code == 200, "admin rejects with a note")
l2 = [l for l in agentA.get("/api/agent/me").json()["listings"] if l["id"] == lid2][0]
check(l2["status"] == "rejected" and l2["review_note"] == "Please add a clearer cover photo", "agent sees rejection note")
check(agentB.post(f"/api/agent/listings/{lid}/withdraw").status_code == 404, "another agent can't withdraw it")
check(agentA.post(f"/api/agent/listings/{lid}/withdraw").status_code == 200, "owner withdraws (sold)")
check(all(l["title"] != "Farmhouse with mountain views" for l in srch(anon)) and anon.get(f"/api/listing/{lid}").status_code == 404, "withdrawn listing gone from search and its page")
check(admin.post(f"/api/admin/listings/{lid}/approve").status_code == 404, "withdrawn listing can't be re-approved by mistake")

# --- login redirect guard
ok_next = lambda n: bool(re.fullmatch(r"/[A-Za-z0-9/_\-?=&.%]*", n or "")) and not n.startswith("//")
check(ok_next("/agents") and not ok_next("//evil.com") and not ok_next("https://evil.com") and not ok_next("/\\evil.com"), "login ?next= only accepts same-site paths")

print("FAILURES:", fails or "none"); sys.exit(1 if fails else 0)
