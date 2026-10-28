import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
import json, base64
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya", SECRET_KEY="testsecret",
                  ADMIN_EMAILS="admin@example.com", CONTACT_EMAIL="hello@akiyafind.com", BASE_URL="https://akiyafind.com")

import psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS agent_feeds, enquiries, agent_photos, agent_listings, agent_profiles, saved_searches, listings, users, crawl_log CASCADE")
cur.execute("""CREATE TABLE listings (id SERIAL PRIMARY KEY, title_en TEXT, title_jp TEXT, prefecture TEXT, city TEXT,
  price_jpy BIGINT, size_m2 FLOAT, source_name TEXT, source_url TEXT UNIQUE, is_free BOOLEAN, lat FLOAT, lng FLOAT, image_url TEXT)""")
# agent_listings as it exists in production today, before the feed columns
cur.execute("""CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, google_id TEXT UNIQUE, name TEXT, picture TEXT,
  tier TEXT DEFAULT 'free', stripe_customer_id TEXT, stripe_sub_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""")
cur.execute("""CREATE TABLE agent_listings (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending', title TEXT NOT NULL, prefecture TEXT NOT NULL, city TEXT NOT NULL DEFAULT '',
  price_jpy BIGINT, land_m2 DOUBLE PRECISION, building_m2 DOUBLE PRECISION, year_built INTEGER, layout TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', listing_url TEXT NOT NULL DEFAULT '', review_note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(), reviewed_at TIMESTAMPTZ)""")
conn.commit()

import httpx
from starlette.testclient import TestClient
from itsdangerous import TimestampSigner
import server, agents, feeds

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); cond or fails.append(msg)

def client_for(email):
    c = TestClient(server.app)
    if email:
        u = server.upsert_user("g-" + email, email, email.split("@")[0], "")
        c.cookies.set("session", TimestampSigner("testsecret").sign(base64.b64encode(json.dumps({"user": u}).encode())).decode())
    return c

# --- schema: feed columns added to the existing agent_listings table on startup
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='agent_listings'")
cols = {r[0] for r in cur.fetchall()}; conn.commit()
check({"feed_id", "feed_ref", "photo_urls", "feed_removed_at"} <= cols, "feed columns added to existing agent_listings")
feeds.ensure_feed_schema(cur); feeds.ensure_feed_schema(cur); conn.commit()
check(True, "ensure_feed_schema is idempotent")

# --- address safety (real resolver, IP literals need no DNS)
real_public = feeds.host_is_public
check(not real_public("127.0.0.1") and not real_public("10.1.2.3") and not real_public("169.254.169.254")
      and not real_public("::1") and not real_public("192.168.0.1") and not real_public("localhost"), "loopback, private and metadata addresses refused")
check(real_public("8.8.8.8"), "public address allowed")
PUBLIC = {"feeds.example", "img.shiawase.example", "shiawase.example"}
feeds.host_is_public = lambda h: h in PUBLIC
check(not feeds.url_is_fetchable("ftp://feeds.example/x") and not feeds.url_is_fetchable("https://u:p@feeds.example/x")
      and not feeds.url_is_fetchable("https://feeds.example:8443/x") and not feeds.url_is_fetchable("https://elsewhere.example/x")
      and feeds.url_is_fetchable("https://feeds.example/x.xml"), "only plain http(s) URLs on public hosts are fetched")

# --- a fake web for the feed fetcher
class Resp:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code, self.body = status, body
        self.headers = {"location": location} if location else {}
        self.is_redirect = location is not None
    def iter_content(self, n): return [self.body[i:i + n] for i in range(0, len(self.body), n)] or [b""]
    def __enter__(self): return self
    def __exit__(self, *a): return False
WEB, requested = {}, []
def fake_get(url, **kw):
    requested.append(url)
    r = WEB.get(url)
    return r() if callable(r) else (r or Resp(404))
feeds.requests.get = fake_get

WEB["https://feeds.example/redir.xml"] = Resp(302, location="http://127.0.0.1:8000/admin")
requested.clear()
try: feeds.fetch_url("https://feeds.example/redir.xml"); check(False, "redirect to internal address refused")
except feeds.FeedError: check(not any("127.0.0.1" in u for u in requested), "redirect to internal address refused, never requested")
WEB["https://feeds.example/big.xml"] = Resp(200, b"x" * 5000)
try: feeds.fetch_url("https://feeds.example/big.xml", max_bytes=1000); check(False, "oversized feed refused")
except feeds.FeedError as e: check("larger" in str(e), "oversized feed refused")
try: feeds.fetch_url("https://feeds.example/missing.xml"); check(False, "HTTP error reported")
except feeds.FeedError as e: check("404" in str(e), "HTTP error reported")

# --- parsing
def prop(id_="", price="", currency="JPY", freq="sale", kind="house", town="", province="", country="Japan",
         beds="", baths="", built="", plot="", url="", desc="", images=(), title=""):
    imgs = "".join(f'<image id="{i}"><url>{u}</url></image>' for i, u in images)
    return (f"<property><id>{id_}</id><price>{price}</price><currency>{currency}</currency><price_freq>{freq}</price_freq>"
            f"<type>{kind}</type><town>{town}</town><province>{province}</province><country>{country}</country>"
            f"<beds>{beds}</beds><baths>{baths}</baths><surface_area><built>{built}</built><plot>{plot}</plot></surface_area>"
            + (f"<title><en>{title}</en></title>" if title else "")
            + f"<url><en>{url}</en></url><desc>{desc}</desc><images>{imgs}</images></property>")
def feed(*props): return ("<?xml version='1.0' encoding='utf-8'?><root><kyero><feed_version>3</feed_version></kyero>"
                          + "".join(props) + "</root>").encode()
A1 = dict(id_="A1", price="3,500,000", town="Myoko", province="Niigata", beds="4", baths="1", built="120", plot="300",
          url="https://shiawase.example/p/a1", desc="<en>&lt;p&gt;Big &amp;amp; old house.&lt;/p&gt;&lt;p&gt;Near the ski slopes&lt;/p&gt;</en>",
          images=[(2, "https://img.shiawase.example/a1-2.jpg"), (1, "https://img.shiawase.example/a1-1.jpg"), (3, "javascript:alert(1)")])
A2 = dict(id_="A2", price="50000", currency="EUR", town="上越市", province="新潟県", images=[(1, "https://img.shiawase.example/a2.jpg")])
A3 = dict(id_="A3", price="0", kind="land", town="Hakuba", province="Nagano-ken", plot="800")
A7 = dict(id_="A7", price="9800000", town="Sado", province="Niigata Prefecture", title="Kominka with\n garden", desc="<ja>古民家です</ja>")
FULL = feed(prop(**A1), prop(**A2), prop(**A3), prop(**A7),
            prop(id_="A4", price="80000", freq="month", province="Niigata"),
            prop(id_="A5", price="1000000", province="Narnia"),
            prop(**{**A1, "price": "1"}),  # duplicate id
            prop(id_="A6", price="100", province="Alicante", country="Spain"),
            prop(price="5", province="Niigata"))
items, skipped, notes = feeds.parse_kyero(FULL)
by = {i["ref"]: i for i in items}
check(sorted(by) == ["A1", "A2", "A3", "A7"], f"4 properties imported ({sorted(by)})")
check(dict(skipped) == {"for rent, not for sale": 1, "prefecture not recognised": 1, "duplicate id": 1, "not in Japan": 1, "no id": 1},
      f"rentals, unknown prefectures, duplicates, other countries and missing ids skipped ({dict(skipped)})")
a1 = by["A1"]
check(a1["title"] == "4-bedroom house in Myoko, Niigata" and a1["prefecture"] == "Niigata" and a1["price_jpy"] == 3500000
      and a1["layout"] == "4 bed · 1 bath" and a1["land_m2"] == 300 and a1["building_m2"] == 120, f"fields mapped ({a1['title']}, {a1['layout']})")
check(a1["photo_urls"] == ["https://img.shiawase.example/a1-1.jpg", "https://img.shiawase.example/a1-2.jpg"], "photos in id order, non-http dropped")
check(a1["description"] == "Big & old house.\nNear the ski slopes", f"HTML stripped from description ({a1['description']!r})")
check(by["A2"]["price_jpy"] is None and notes["price not in yen, shown as Price on request"] == 1 and by["A2"]["prefecture"] == "Niigata",
      "euro price never shown as yen; Japanese prefecture name understood")
check(by["A3"]["price_jpy"] is None and by["A3"]["title"] == "Land in Hakuba, Nagano", "price 0 becomes Price on request, never FREE; land titled")
check(by["A7"]["title"] == "Kominka with garden" and by["A7"]["description"] == "古民家です" and by["A7"]["photo_urls"] == [],
      "feed title used (kept on one line); Japanese description used when there's no English")
for bad, why in [(b"<!DOCTYPE r [<!ENTITY a 'aaaa'><!ENTITY b '&a;&a;&a;&a;'>]><root><property><id>&b;</id></property></root>", "entity expansion"),
                 (b"<!DOCTYPE r [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><root><property><id>&x;</id></property></root>", "external entity"),
                 (b"not xml at all", "malformed XML"), (b"<root><item/></root>", "no properties")]:
    try: feeds.parse_kyero(bad); check(False, f"{why} refused")
    except feeds.FeedError: check(True, f"{why} refused")

# --- agents add feeds; nothing is fetched before approval
anon, agentA, agentB, admin = client_for(None), client_for("a@shiawase.example"), client_for("b@other.example"), client_for("admin@example.com")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, FULL)
check(agentA.post("/api/agent/feeds", json={"url": "https://shiawase.example/kyero.xml"}).status_code == 400, "can't add a feed before company details")
agentA.put("/api/agent/profile", json={"company": "Shiawase Home", "email": "a@shiawase.example", "license_no": "新潟県知事(1)第1号"})
check(agentA.post("/api/agent/feeds", json={"url": "javascript:alert(1)"}).status_code == 400, "non-http feed address rejected")
r = agentA.post("/api/agent/feeds", json={"url": "https://shiawase.example/kyero.xml"}); check(r.json()["status"] == "pending", "feed added -> pending")
check(agentA.post("/api/agent/feeds", json={"url": "https://shiawase.example/kyero.xml"}).json()["status"] == "already added", "same feed twice -> already added")
fid = agentA.get("/api/agent/feeds").json()["feeds"][0]["id"]
requested.clear(); check(feeds.main() == 0 and not requested, "daily run ignores pending feeds (nothing fetched)")
check(anon.get("/api/admin/feeds").status_code == 404 and agentA.post(f"/api/admin/feeds/{fid}/approve").status_code == 404,
      "agents and visitors can't see or approve feeds")

# --- approve -> import
r = admin.post(f"/api/admin/feeds/{fid}/approve").json()
check(r["ok"] and "4 listings imported" in r["message"] and "1 skipped: for rent" in r["message"] and "price not in yen" in r["message"], f"approve imports ({r['message']})")
srch = lambda pref: {l["title"]: l for l in anon.get("/api/search", params={"prefecture": pref}).json()["listings"]}
n = srch("Niigata"); hit = n.get("4-bedroom house in Myoko, Niigata")
check(hit and hit["image_url"] == "https://img.shiawase.example/a1-1.jpg" and hit["source_name"] == "Shiawase Home"
      and hit["price_jpy"] == 3500000 and hit["source_url"].startswith("https://akiyafind.com/listing/"), "feed listing in search with photo, company and its own page")
check(srch("Nagano").get("Land in Hakuba, Nagano", {}).get("price_jpy", "x") is None, "price on request stays null in search")
cur.execute("SELECT feed_ref, id FROM agent_listings WHERE feed_id=%s", (fid,)); ids = dict(cur.fetchall()); conn.commit()
pub = anon.get(f"/api/listing/{ids['A1']}").json()
check(pub["photo_urls"] == [agents.image_proxy_path("https://img.shiawase.example/a1-1.jpg"), agents.image_proxy_path("https://img.shiawase.example/a1-2.jpg")]
      and pub["from_feed"] and pub["agent"]["company"] == "Shiawase Home" and pub["listing_url"] == "https://shiawase.example/p/a1",
      "listing page shows proxied photos, agent and link to the agent's page")
af = admin.get("/api/admin/feeds").json()["feeds"][0]
check(af["status"] == "approved" and af["live"] == 4 and af["last_ok"] and af["agent"]["company"] == "Shiawase Home", "admin sees feed with 4 live listings")

# --- image proxy: feed photo hosts only, and only public ones
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
def img_handler(req):
    if req.url.host == "img.shiawase.example": return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    return httpx.Response(404)
Real = httpx.AsyncClient
server.httpx.AsyncClient = lambda *a, **k: Real(transport=httpx.MockTransport(img_handler))
img = lambda u: anon.get("/api/img", params={"url": u})
r = img("https://img.shiawase.example/a1-1.jpg"); check(r.status_code == 200 and r.content == PNG, "photo from an approved feed's host proxied")
check(img("https://img.unlisted.example/x.jpg").status_code == 400, "host not in any approved feed refused")
PUBLIC.discard("img.shiawase.example")
check(img("https://img.shiawase.example/a1-1.jpg").status_code == 400, "feed host that resolves to a private address refused")
PUBLIC.add("img.shiawase.example")

# --- enquiries on feed listings, and the new email format
sent = []
class R:
    def __init__(self, code): self.status_code = code
codes = []
agents.requests.post = lambda url, **kw: (sent.append(kw["json"]), R(codes.pop(0) if codes else 200))[1]
agents.RESEND_API_KEY = "re_test"; agents._enquiry_times.clear()
r = anon.post(f"/api/listing/{ids['A1']}/enquire", json={"name": "Jane Buyer", "email": "jane@example.com", "message": "Is it still available?"})
m = sent[-1]
check(r.status_code == 200 and m["to"] == ["a@shiawase.example"] and m["reply_to"] == "jane@example.com", "feed listing enquiry emailed to the agent, reply-to the buyer")
check(m["from"] == '"Jane Buyer via AkiyaFind" <alerts@akiyafind.com>' and m["subject"] == "Enquiry about 4-bedroom house in Myoko, Niigata",
      f"email comes 'from' the buyer via AkiyaFind ({m['from']} / {m['subject']})")
check(m["text"].startswith("Is it still available?") and m["text"].count("https://") == 1 and f"/listing/{ids['A1']}" in m["text"],
      "buyer's message first, a single link")
sent.clear(); codes[:] = [422, 200]
anon.post(f"/api/listing/{ids['A1']}/enquire", json={"name": 'Evil" <x@y.z>, Bob', "email": "e@example.com", "message": "hi"})
check(len(sent) == 2 and sent[0]["from"].count("<") == 1 and sent[0]["from"].count('"') == 2 and "," not in sent[0]["from"]
      and sent[1]["from"] == "AkiyaFind <alerts@akiyafind.com>", f"name can't break the From; rejected From retried with the plain sender ({sent[0]['from']})")
cur.execute("SELECT emailed FROM enquiries ORDER BY id DESC LIMIT 1"); check(cur.fetchone()[0] is True, "retried enquiry marked emailed"); conn.commit()

# --- agent withdraws one feed listing: it stays withdrawn
check(agentA.post(f"/api/agent/listings/{ids['A3']}/withdraw").status_code == 200, "agent withdraws a feed listing")
mine = {l["title"]: l for l in agentA.get("/api/agent/me").json()["listings"]}
check(len(mine) == 4 and mine["4-bedroom house in Myoko, Niigata"]["from_feed"], "agent dashboard lists feed listings, marked as from the feed")

# --- re-import: price change, a sold property, the withdrawn one stays withdrawn
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**{**A1, "price": "3000000"}), prop(**A3), prop(**A7)))
r = admin.post(f"/api/admin/feeds/{fid}/import").json()
check(r["ok"] and "1 no longer in the feed, hidden" in r["message"], f"re-import hides the property that left the feed ({r['message']})")
n = srch("Niigata")
check(n["4-bedroom house in Myoko, Niigata"]["price_jpy"] == 3000000, "price change picked up")
check(not any("上越" in t for t in n) and anon.get(f"/api/listing/{ids['A2']}").status_code == 404
      and anon.post(f"/api/listing/{ids['A2']}/enquire", json={"name": "x", "email": "x@x.com", "message": "hi"}).status_code == 404,
      "property that left the feed: gone from search, its page and enquiries")
check(agentA.get(f"/api/listing/{ids['A2']}").status_code == 200 and not agentA.get(f"/api/listing/{ids['A2']}").json()["in_feed"], "owner can still see it, marked not in feed")
check("Land in Hakuba, Nagano" not in srch("Nagano") and anon.get(f"/api/listing/{ids['A3']}").status_code == 404, "withdrawn feed listing stays off after re-import")

# --- a feed that suddenly shrinks is treated as broken, not sold out
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1)))
r = admin.post(f"/api/admin/feeds/{fid}/import").json()
check(r["ok"] and "kept" in r["message"] and "after 2 more imports" in r["message"] and "Kominka with garden" in srch("Niigata"),
      f"feed shrank by more than half: listings kept ({r['message']})")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1), prop(**A2), prop(**A7)))
admin.post(f"/api/admin/feeds/{fid}/import")
check(anon.get(f"/api/listing/{ids['A2']}").status_code == 200, "property that comes back into the feed is shown again")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1)))
admin.post(f"/api/admin/feeds/{fid}/import"); r = admin.post(f"/api/admin/feeds/{fid}/import").json()
check("Kominka with garden" in srch("Niigata") and "after 1 more import" in r["message"], f"still small on the 2nd import: still kept ({r['message']})")
r = admin.post(f"/api/admin/feeds/{fid}/import").json()
check("Kominka with garden" not in srch("Niigata") and "2 no longer in the feed" in r["message"], f"3rd import in a row: believed, sold listings hidden ({r['message']})")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1), prop(**A2), prop(**A7)))
admin.post(f"/api/admin/feeds/{fid}/import")
check("Kominka with garden" in srch("Niigata"), "feed back to normal: listings shown again")
cur.execute("UPDATE agent_listings SET status='rejected' WHERE id=%s", (ids["A2"],)); conn.commit()
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1), prop(**A3), prop(**A7)))
admin.post(f"/api/admin/feeds/{fid}/import")
admin.post(f"/api/admin/listings/{ids['A2']}/approve")
check(not any("上越" in t for t in srch("Niigata")), "approving a single listing that left its feed doesn't put it back in search")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1), prop(**A2), prop(**A7)))
admin.post(f"/api/admin/feeds/{fid}/import")
NS = feed(prop(**A1)).replace(b"<root>", b"<root xmlns='http://kyero.com/feed'>")
check([i["ref"] for i in feeds.parse_kyero(NS)[0]] == ["A1"], "feed with an XML namespace still read")

# --- broken feed: error recorded, listings untouched
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(500)
r = admin.post(f"/api/admin/feeds/{fid}/import").json()
af = admin.get("/api/admin/feeds").json()["feeds"][0]
check(not r["ok"] and not af["last_ok"] and "500" in af["last_message"] and "4-bedroom house in Myoko, Niigata" in srch("Niigata"),
      "feed down: failure shown to admin, listings left as they were")
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, b"<html>maintenance</html>")
check(not admin.post(f"/api/admin/feeds/{fid}/import").json()["ok"] and "Kominka with garden" in srch("Niigata"), "HTML instead of a feed: same")

# --- admin switches the feed off, then back on
WEB["https://shiawase.example/kyero.xml"] = lambda: Resp(200, feed(prop(**A1), prop(**A2), prop(**A7)))
admin.post(f"/api/admin/feeds/{fid}/disable")
check(not any(l in srch("Niigata") for l in ("4-bedroom house in Myoko, Niigata", "Kominka with garden"))
      and anon.get(f"/api/listing/{ids['A1']}").status_code == 404, "switched-off feed: all its listings hidden")
check(img("https://img.shiawase.example/a1-1.jpg").status_code == 400, "switched-off feed's photo host no longer proxied")
check(admin.post(f"/api/admin/feeds/{fid}/import").status_code == 400, "can't import a switched-off feed")
admin.post(f"/api/admin/feeds/{fid}/approve")
check("4-bedroom house in Myoko, Niigata" in srch("Niigata"), "re-approved feed: listings back")

# --- agent removes their feed
check(agentB.post(f"/api/agent/feeds/{fid}/remove").status_code == 404, "another agent can't remove it")
check(agentA.post(f"/api/agent/feeds/{fid}/remove").json()["status"] == "removed" and "Kominka with garden" not in srch("Niigata"), "agent removes feed: listings hidden")
check(admin.post(f"/api/admin/feeds/{fid}/approve").status_code == 400, "admin can't re-approve a feed the agent removed")
check(agentA.post("/api/agent/feeds", json={"url": "https://shiawase.example/kyero.xml"}).json()["status"] == "pending", "agent re-adds it -> back to review")
for i in range(2): agentA.post("/api/agent/feeds", json={"url": f"https://shiawase.example/{i}.xml"})
check(agentA.post("/api/agent/feeds", json={"url": "https://shiawase.example/9.xml"}).status_code == 400, "max 3 feeds per agent")

# --- admin adds a feed for an agent who agreed by email
WEB["https://feeds.example/hakuba.xml"] = lambda: Resp(200, feed(prop(id_="H1", price="12000000", town="Hakuba", province="Nagano", images=[(1, "https://img.shiawase.example/h1.jpg")])))
body = {"company": "Hakuba Homes", "email": "Info@HakubaHomes.example", "license_no": "長野県知事(1)第999号", "url": "https://feeds.example/hakuba.xml"}
check(agentA.post("/api/admin/feeds", json=body).status_code == 404, "only the admin can add feeds for others")
r = admin.post("/api/admin/feeds", json=body).json()
check(r["ok"] and "1 listing imported" in r["message"] and "House in Hakuba, Nagano" in srch("Nagano"), f"admin-added feed imported ({r['message']})")
hh = client_for("info@hakubahomes.example")  # they sign in with Google later
me = hh.get("/api/agent/me").json()
check(me["profile"]["company"] == "Hakuba Homes" and len(me["listings"]) == 1 and hh.get("/api/agent/feeds").json()["feeds"][0]["status"] == "approved",
      "agent signing in later owns the profile, feed and listings")
admin.post("/api/admin/feeds", json={**body, "company": "Overwritten Ltd", "url": "https://feeds.example/hakuba2.xml"})
check(hh.get("/api/agent/me").json()["profile"]["company"] == "Hakuba Homes", "admin adding another feed never overwrites the agent's own details")
check(admin.post("/api/admin/feeds", json={**body, "url": "https://shiawase.example/kyero.xml", "email": "a@shiawase.example"}).status_code == 200,
      "admin can add a feed for an existing agent")
agentA.post(f"/api/agent/feeds/{fid}/remove")
check(admin.post("/api/admin/feeds", json={**body, "url": "https://shiawase.example/kyero.xml", "email": "a@shiawase.example"}).status_code == 400,
      "admin form can't switch back on a feed the agent removed")

# --- daily run: one broken feed doesn't stop the others; exit code flags it
WEB["https://feeds.example/hakuba.xml"] = lambda: Resp(200, feed(prop(id_="H1", price="11000000", town="Hakuba", province="Nagano"),
                                                               prop(id_="H2", price="5000000", town="Otari", province="Nagano")))
check(feeds.main() == 1, "daily run exits 1 when a feed failed (hakuba2.xml doesn't exist)")
check("House in Otari, Nagano" in srch("Nagano") and srch("Nagano")["House in Hakuba, Nagano"]["price_jpy"] == 11000000, "...and still imports the feeds that work")

print("FAILURES:", fails or "none"); sys.exit(1 if fails else 0)
