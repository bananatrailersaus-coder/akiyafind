"""
feeds.py — import agents' listings feeds (Kyero XML, version 3) into AkiyaFind.

An agent adds their feed URL on /agents, or the admin adds it for them from
/admin once they've agreed by email. Nothing is fetched until the admin
approves the feed: approval is our record that the agent gave permission.
Approved feeds are imported straight away, then daily by the GitHub workflow
(`python3 feeds.py`), and whenever the admin clicks "Import now".

Each property becomes an approved agent listing owned by that agent, so it
gets a /listing page with the enquiry form and is mirrored into `listings`
like any other agent listing. Photos stay on the agent's server and are shown
through /api/img, which only proxies hosts that appear in approved feeds.
"""

import html
import ipaddress
import os
import re
import socket
import sys
import time
import unicodedata
from collections import Counter
from urllib.parse import urljoin, urlparse

import psycopg2
import requests
from bs4 import BeautifulSoup
from defusedxml import ElementTree as SafeET
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from agents import (EMAIL_RE, MAX_PHOTOS, PREFECTURES, get_db, publish, require_admin, require_user,
                    text, unpublish, url_or_blank)

router = APIRouter()

MAX_FEED_BYTES = 30 * 1024 * 1024
MAX_FEED_ITEMS = 2000
MAX_FEEDS_PER_AGENT = 3
MIN_SEEN_RATIO = 0.5  # same rule as the crawler: a feed that suddenly shrinks by half is more likely broken than sold out
SHRINK_RUNS_TO_ACCEPT = 3  # ...but if it's still that small three imports in a row, believe it
USER_AGENT = "AkiyaFindBot/1.0 (+https://akiyafind.com/agents)"


class FeedError(Exception):
    """A feed couldn't be fetched or read. The message is shown to the admin."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# agent_listings columns for imported properties. photo_urls: the feed's photo
# URLs (uploaded listings use agent_photos instead). feed_removed_at: set when
# the property drops out of the feed or the feed is switched off; the listing
# is hidden until it reappears.
FEED_LISTING_COLUMNS = {
    "feed_id":         "INTEGER",
    "feed_ref":        "TEXT",
    "photo_urls":      "TEXT[] NOT NULL DEFAULT '{}'",
    "feed_removed_at": "TIMESTAMPTZ",
}


def ensure_feed_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_feeds (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url          TEXT NOT NULL,
            format       TEXT NOT NULL DEFAULT 'kyero',
            status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | disabled (by admin) | removed (by agent)
            review_note  TEXT NOT NULL DEFAULT '',
            photo_hosts  TEXT[] NOT NULL DEFAULT '{}',
            last_run_at  TIMESTAMPTZ,
            last_ok      BOOLEAN,
            last_message TEXT NOT NULL DEFAULT '',
            last_count   INTEGER,
            shrink_runs  INTEGER NOT NULL DEFAULT 0,  -- imports in a row that kept listings because the feed shrank
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            updated_at   TIMESTAMPTZ DEFAULT NOW(),
            reviewed_at  TIMESTAMPTZ,
            UNIQUE (user_id, url)
        )
    """)
    # Same reason as schema.py: ALTER and CREATE INDEX lock the table even when
    # there's nothing to do, so check first.
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'agent_listings'""")
    existing = {r[0] for r in cur.fetchall()}
    missing = [c for c in FEED_LISTING_COLUMNS if c not in existing]
    cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'agent_listings_feed_ref'")
    need_index = cur.fetchone() is None
    if missing or need_index:
        cur.execute("SET LOCAL lock_timeout = '10s'")
    for col in missing:
        cur.execute(f"ALTER TABLE agent_listings ADD COLUMN IF NOT EXISTS {col} {FEED_LISTING_COLUMNS[col]}")
    if need_index:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_listings_feed_ref "
                    "ON agent_listings (feed_id, feed_ref) WHERE feed_id IS NOT NULL")


# ---------------------------------------------------------------------------
# Fetching agent-supplied URLs safely
# ---------------------------------------------------------------------------
def host_is_public(host):
    """True only if every address the host resolves to is on the public internet,
    so a feed or photo URL can't make our server call localhost or a private network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


def url_is_fetchable(url):
    p = urlparse(url)
    return (p.scheme in ("http", "https") and bool(p.hostname) and p.username is None
            and p.port in (None, 80, 443) and host_is_public(p.hostname))


def fetch_url(url, max_bytes=MAX_FEED_BYTES, timeout=60, deadline=180):
    """GET an agent-supplied URL. Redirects are followed by hand so every hop is re-checked."""
    give_up = time.monotonic() + deadline  # timeout is per read; this caps a server that drips data
    for _ in range(5):
        if not url_is_fetchable(url):
            raise FeedError("The feed address isn't a public web address")
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                             allow_redirects=False, stream=True)
        except requests.RequestException as e:
            raise FeedError(f"Couldn't connect to the feed ({type(e).__name__})")
        with r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status_code != 200:
                raise FeedError(f"The feed returned HTTP {r.status_code}")
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > max_bytes:
                    raise FeedError(f"The feed is larger than {max_bytes // (1024 * 1024)} MB")
                if time.monotonic() > give_up:
                    raise FeedError("The feed took too long to download")
            return data
    raise FeedError("The feed redirected too many times")


# ---------------------------------------------------------------------------
# Reading Kyero XML
# ---------------------------------------------------------------------------
JP_PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県", "埼玉県",
    "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県",
    "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県",
    "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県", "熊本県", "大分県",
    "宮崎県", "鹿児島県", "沖縄県",
)  # same order as PREFECTURES


def _romaji_key(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()  # Kōchi -> kochi
    s = re.sub(r"\s*prefecture$", "", s.strip())                                    # Niigata Prefecture
    s = re.sub(r"[\s-]+(ken|fu|to)$", "", s)                                        # Kyoto-fu -> kyoto (not Kyoto -> kyo)
    s = re.sub(r"[^a-z]", "", s)
    return s.replace("ou", "o").replace("oo", "o")                                 # Hyougo, Ooita -> hyogo, oita


_PREF_KEYS = {_romaji_key(p): p for p in PREFECTURES}
_PREF_KEYS["gumma"] = "Gunma"
_PREF_JP = {}
for _jp, _en in zip(JP_PREFECTURES, PREFECTURES):
    _PREF_JP[_jp] = _en
    if _jp != "北海道":
        _PREF_JP[_jp[:-1]] = _en  # 新潟 as well as 新潟県


def match_prefecture(value):
    v = (value or "").strip()
    if not v:
        return None
    if v in _PREF_JP:
        return _PREF_JP[v]
    for jp, en in _PREF_JP.items():  # 新潟県妙高市 -> Niigata
        if v.startswith(jp):
            return en
    return _PREF_KEYS.get(_romaji_key(v))


def _text(el, path):
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def _lang_text(el, tag):
    """Kyero puts translations in child elements (<desc><en>…</en><ja>…</ja></desc>); prefer English."""
    node = el.find(tag)
    if node is None:
        return ""
    kids = list(node)
    if not kids:
        return (node.text or "").strip()
    for lang in ("en", "ja", "jp"):
        t = _text(node, lang)
        if t:
            return t
    return next(((k.text or "").strip() for k in kids if (k.text or "").strip()), "")


def _number(value, kind=float):
    v = re.sub(r"[^\d.]", "", value or "")
    try:
        return kind(float(v)) if v else None
    except ValueError:
        return None


def _http_url(value):
    v = (value or "").strip()
    return v if re.match(r"^https?://\S+$", v, re.I) and len(v) <= 500 else ""


def _clean_description(raw):
    if not raw:
        return ""
    t = BeautifulSoup(html.unescape(raw), "html.parser").get_text("\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()
    return t[:5000]


def _title(kind, beds, town, prefecture):
    place = ", ".join(x for x in (town, prefecture) if x)
    kind = (kind or "").strip().lower()
    if kind in ("land", "plot", "lot"):
        return f"Land in {place}"
    word = kind if re.fullmatch(r"[a-z][a-z \-]{1,30}", kind) else "property"
    head = f"{beds}-bedroom {word}" if beds else word
    return f"{head[0].upper()}{head[1:]} in {place}"[:150]


YEN = ("JPY", "YEN", "¥", "円")


def parse_kyero(data):
    """Returns (properties, skipped, notes). skipped/notes count why properties were left out or adjusted."""
    try:
        root = SafeET.fromstring(data)
    except Exception as e:  # defusedxml refuses DTDs and entity tricks; anything else is malformed XML
        raise FeedError(f"The feed isn't valid XML ({type(e).__name__})")
    for el in root.iter():  # ignore any XML namespace so <kyero:property> reads as <property>
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    props = root.findall(".//property")
    if not props:
        raise FeedError("The feed has no <property> entries. Is it a Kyero feed?")
    out, seen = [], set()
    skipped, notes = Counter(), Counter()
    for p in props[:MAX_FEED_ITEMS]:
        ref = (_text(p, "id") or _text(p, "ref"))[:100]
        if not ref:
            skipped["no id"] += 1
            continue
        if ref in seen:
            skipped["duplicate id"] += 1
            continue
        if (_text(p, "price_freq") or "sale").lower() != "sale":
            skipped["for rent, not for sale"] += 1
            continue
        country = _text(p, "country").lower()
        if country and country not in ("japan", "jp", "jpn", "日本"):
            skipped["not in Japan"] += 1
            continue
        town = _text(p, "town")
        pref = match_prefecture(_text(p, "province")) or match_prefecture(town) or match_prefecture(_text(p, "location_detail"))
        if not pref:
            skipped["prefecture not recognised"] += 1
            continue
        price = _number(_text(p, "price"), int)
        currency = _text(p, "currency").upper()
        if price is not None and currency not in YEN:
            # Never guess the currency: a euro price shown as yen would be badly wrong.
            notes["price not in yen, shown as Price on request"] += 1
            price = None
        if price == 0:
            price = None  # 0 means "price on request" in feeds; it must never show as FREE
        if price is not None and price > 100_000_000_000:
            price = None
        beds = _number(_text(p, "beds"), int)
        baths = _number(_text(p, "baths"), int)
        built = _number(_text(p, "surface_area/built"))
        plot = _number(_text(p, "surface_area/plot"))
        images = []
        for i, img in enumerate(p.findall("images/image")):
            u = _http_url(_text(img, "url") or (img.text or ""))
            if u:
                n = _number(img.get("id") or "", int)
                images.append((n if n is not None else 10_000 + i, u))
        photo_urls = [u for _, u in sorted(images)][:MAX_PHOTOS]
        layout = " · ".join(x for x in (f"{beds} bed" if beds else "", f"{baths} bath" if baths else "") if x)
        seen.add(ref)
        town = re.sub(r"\s+", " ", town)  # titles and towns end up in email subjects: keep them on one line
        out.append({
            "ref": ref,
            "title": re.sub(r"\s+", " ", _lang_text(p, "title") or _title(_text(p, "type"), beds, town, pref))[:150],
            "prefecture": pref,
            "city": town[:100],
            "price_jpy": price,
            "land_m2": plot if plot and plot < 10_000_000 else None,
            "building_m2": built if built and built < 1_000_000 else None,
            "layout": layout[:50],
            "description": _clean_description(_lang_text(p, "desc")),
            "listing_url": _http_url(_lang_text(p, "url")),
            "photo_urls": photo_urls,
        })
    if len(props) > MAX_FEED_ITEMS:
        skipped[f"over the {MAX_FEED_ITEMS}-property limit"] += len(props) - MAX_FEED_ITEMS
    return out, skipped, notes


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------
def _hide_feed_listings(cur, feed_id, refs=None):
    """Hide a feed's listings (all, or just these refs) without forgetting agent/admin decisions."""
    if refs is None:
        cur.execute("UPDATE agent_listings SET feed_removed_at = NOW() WHERE feed_id = %s AND feed_removed_at IS NULL "
                    "RETURNING id", (feed_id,))
    else:
        cur.execute("UPDATE agent_listings SET feed_removed_at = NOW() WHERE feed_id = %s AND feed_ref = ANY(%s) "
                    "AND feed_removed_at IS NULL RETURNING id", (feed_id, list(refs)))
    ids = [r[0] for r in cur.fetchall()]
    for lid in ids:
        unpublish(cur, lid)
    return len(ids)


def _summary(imported, removed, skipped, notes, kept=0, runs_left=0):
    parts = [f"{imported} listing{'' if imported == 1 else 's'} imported"]
    if removed:
        parts.append(f"{removed} no longer in the feed, hidden")
    if kept:
        parts.append(f"{kept} missing from the feed but kept, because the feed shrank by more than half "
                     f"(they'll be hidden if it's still this small after {runs_left} more import{'' if runs_left == 1 else 's'})")
    parts += [f"{n} skipped: {why}" for why, n in skipped.most_common()]
    parts += [f"{n} {why}" for why, n in notes.most_common()]
    return "; ".join(parts)


def sync_feed(conn, feed_id):
    """Import one approved feed. Returns (ok, message). Never raises for feed problems."""
    cur = conn.cursor()
    cur.execute("SELECT f.url, f.status, f.user_id, f.shrink_runs FROM agent_feeds f "
                "JOIN agent_profiles p ON p.user_id = f.user_id WHERE f.id = %s", (feed_id,))
    row = cur.fetchone()
    if not row or row[1] != "approved":
        cur.close()
        return False, "Feed not found or not approved"
    url, _, user_id, shrink_runs = row
    try:
        items, skipped, notes = parse_kyero(fetch_url(url))
    except FeedError as e:
        # Leave the listings as they were: a feed that's down for a day shouldn't empty the site.
        cur.execute("UPDATE agent_feeds SET last_run_at = NOW(), last_ok = FALSE, last_message = %s WHERE id = %s",
                    (str(e), feed_id))
        conn.commit()
        cur.close()
        return False, str(e)

    cur.execute("SELECT feed_ref FROM agent_listings WHERE feed_id = %s AND feed_removed_at IS NULL", (feed_id,))
    active = {r[0] for r in cur.fetchall()}
    for it in items:
        cur.execute("""
            INSERT INTO agent_listings (user_id, status, title, prefecture, city, price_jpy, land_m2, building_m2,
                                        layout, description, listing_url, photo_urls, feed_id, feed_ref, reviewed_at)
            VALUES (%s, 'approved', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (feed_id, feed_ref) WHERE feed_id IS NOT NULL DO UPDATE SET
                title = EXCLUDED.title, prefecture = EXCLUDED.prefecture, city = EXCLUDED.city,
                price_jpy = EXCLUDED.price_jpy, land_m2 = EXCLUDED.land_m2, building_m2 = EXCLUDED.building_m2,
                layout = EXCLUDED.layout, description = EXCLUDED.description, listing_url = EXCLUDED.listing_url,
                photo_urls = EXCLUDED.photo_urls, feed_removed_at = NULL, updated_at = NOW()
            RETURNING id, status
        """, (user_id, it["title"], it["prefecture"], it["city"], it["price_jpy"], it["land_m2"], it["building_m2"],
              it["layout"], it["description"], it["listing_url"], it["photo_urls"], feed_id, it["ref"]))
        lid, status = cur.fetchone()
        if status == "approved":
            publish(cur, lid)
        else:
            unpublish(cur, lid)  # the agent withdrew it, or the admin rejected it: that decision sticks

    seen = {it["ref"] for it in items}
    gone = active - seen
    removed = kept = 0
    if gone and (len(seen) >= MIN_SEEN_RATIO * len(active) or shrink_runs + 1 >= SHRINK_RUNS_TO_ACCEPT):
        removed = _hide_feed_listings(cur, feed_id, gone)
        shrink_runs = 0
    elif gone:
        kept = len(gone)
        shrink_runs += 1
    else:
        shrink_runs = 0
    hosts = sorted({urlparse(u).hostname.lower() for it in items for u in it["photo_urls"] if urlparse(u).hostname})
    message = _summary(len(items), removed, skipped, notes, kept, SHRINK_RUNS_TO_ACCEPT - shrink_runs)
    cur.execute("UPDATE agent_feeds SET last_run_at = NOW(), last_ok = TRUE, last_message = %s, last_count = %s, "
                "photo_hosts = %s, shrink_runs = %s WHERE id = %s", (message, len(items), hosts, shrink_runs, feed_id))
    conn.commit()
    cur.close()
    _photo_hosts_cache["at"] = 0  # pick up new photo hosts straight away
    return True, message


# ---------------------------------------------------------------------------
# Which photo hosts /api/img may proxy
# ---------------------------------------------------------------------------
_photo_hosts_cache = {"at": 0, "hosts": frozenset()}


def feed_photo_hosts():
    """Photo hosts of approved feeds, cached for 5 minutes (called by the image proxy)."""
    if time.time() - _photo_hosts_cache["at"] > 300:
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT unnest(photo_hosts) FROM agent_feeds WHERE status = 'approved'")
                _photo_hosts_cache["hosts"] = frozenset(r[0] for r in cur.fetchall())
                _photo_hosts_cache["at"] = time.time()
            finally:
                conn.close()
        except psycopg2.Error:
            pass  # keep serving the last known list rather than failing every photo
    return _photo_hosts_cache["hosts"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
FEED_COLS = "f.id, f.url, f.status, f.review_note, f.last_run_at, f.last_ok, f.last_message, f.last_count, f.created_at"


def feed_dict(row):
    keys = ["id", "url", "status", "review_note", "last_run_at", "last_ok", "last_message", "last_count", "created_at"]
    d = dict(zip(keys, row))
    for k in ("last_run_at", "created_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def feed_url(value):
    v = url_or_blank(value, "Feed address")
    if not v:
        raise HTTPException(status_code=400, detail="Feed address is required")
    return v


@router.get("/api/agent/feeds")
def my_feeds(request: Request):
    user = require_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT {FEED_COLS} FROM agent_feeds f WHERE f.user_id = %s ORDER BY f.id", (user["id"],))
    feeds = [feed_dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"feeds": feeds}


@router.post("/api/agent/feeds")
async def add_feed(request: Request):
    user = require_user(request)
    url = feed_url((await request.json()).get("url"))
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM agent_profiles WHERE user_id = %s", (user["id"],))
        if not cur.fetchone():
            raise HTTPException(status_code=400, detail="Add your company details and licence number first")
        cur.execute("SELECT COUNT(*) FROM agent_feeds WHERE user_id = %s AND status <> 'removed'", (user["id"],))
        if cur.fetchone()[0] >= MAX_FEEDS_PER_AGENT:
            raise HTTPException(status_code=400, detail=f"Up to {MAX_FEEDS_PER_AGENT} feeds per agent")
        # Re-adding a feed that was removed, rejected or switched off sends it back for review.
        cur.execute("""INSERT INTO agent_feeds (user_id, url) VALUES (%s, %s)
                       ON CONFLICT (user_id, url) DO UPDATE SET status = 'pending', updated_at = NOW()
                       WHERE agent_feeds.status IN ('removed', 'rejected', 'disabled')
                       RETURNING id""", (user["id"], url))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"status": "pending" if row else "already added"}


@router.post("/api/agent/feeds/{feed_id}/remove")
def remove_feed(feed_id: int, request: Request):
    user = require_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE agent_feeds SET status = 'removed', updated_at = NOW() WHERE id = %s AND user_id = %s "
                "AND status <> 'removed'", (feed_id, user["id"]))
    found = cur.rowcount
    if found:
        _hide_feed_listings(cur, feed_id)
    conn.commit()
    cur.close()
    conn.close()
    if not found:
        raise HTTPException(status_code=404, detail="Feed not found")
    _photo_hosts_cache["at"] = 0
    return {"status": "removed"}


@router.get("/api/admin/feeds")
def admin_feeds(request: Request):
    require_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"""SELECT {FEED_COLS}, p.company, p.license_no, p.email, p.website,
                           (SELECT COUNT(*) FROM agent_listings l WHERE l.feed_id = f.id
                              AND l.status = 'approved' AND l.feed_removed_at IS NULL)
                    FROM agent_feeds f JOIN agent_profiles p ON p.user_id = f.user_id
                    ORDER BY (f.status = 'pending') DESC, f.id DESC""")
    out = []
    for r in cur.fetchall():
        d = feed_dict(r[:9])
        d["agent"] = dict(zip(["company", "license_no", "email", "website"], r[9:13]))
        d["live"] = r[13]
        out.append(d)
    cur.close()
    conn.close()
    return {"feeds": out}


@router.post("/api/admin/feeds")
async def admin_add_feed(request: Request):
    """Add a feed for an agent who agreed by email and hasn't signed up. If they sign in
    with Google later using this email, the profile, feed and listings are theirs."""
    require_admin(request)
    body = await request.json()
    email = text(body.get("email"), 200, True, "Agent email").lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Email doesn't look right")
    company = text(body.get("company"), 200, True, "Company name")
    license_no = text(body.get("license_no"), 100, True, "Licence number")
    website = url_or_blank(body.get("website"), "Website")
    url = feed_url(body.get("url"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (email, name) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING", (email, company))
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    user_id = cur.fetchone()[0]
    # Never overwrite details an agent entered themselves.
    cur.execute("""INSERT INTO agent_profiles (user_id, company, email, website, license_no)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING""",
                (user_id, company, email, website, license_no))
    cur.execute("""INSERT INTO agent_feeds (user_id, url, status, reviewed_at) VALUES (%s, %s, 'approved', NOW())
                   ON CONFLICT (user_id, url) DO UPDATE SET status = 'approved', reviewed_at = NOW(), updated_at = NOW()
                   WHERE agent_feeds.status <> 'removed'
                   RETURNING id""", (user_id, url))
    row = cur.fetchone()
    conn.commit()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="The agent removed this feed themselves")
    feed_id = row[0]
    cur.close()
    ok, message = await run_in_threadpool(sync_feed, conn, feed_id)  # downloads the feed: keep it off the event loop
    conn.close()
    return {"id": feed_id, "ok": ok, "message": message}


@router.post("/api/admin/feeds/{feed_id}/{action}")
async def admin_feed_action(feed_id: int, action: str, request: Request):
    require_admin(request)
    if action not in ("approve", "import", "reject", "disable"):
        raise HTTPException(status_code=404)
    note = ""
    if action == "reject":
        note = text((await request.json()).get("note"), 1000, label="Note")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status FROM agent_feeds WHERE id = %s", (feed_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Feed not found")
    if (action == "import" and row[0] != "approved") or (action == "approve" and row[0] == "removed"):
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="The agent removed this feed" if row[0] == "removed"
                            else "Approve the feed first")
    if action == "approve":
        cur.execute("UPDATE agent_feeds SET status = 'approved', review_note = '', reviewed_at = NOW(), "
                    "updated_at = NOW() WHERE id = %s", (feed_id,))
    elif action in ("reject", "disable"):
        cur.execute("UPDATE agent_feeds SET status = %s, review_note = %s, reviewed_at = NOW(), updated_at = NOW() "
                    "WHERE id = %s", ("rejected" if action == "reject" else "disabled", note, feed_id))
        _hide_feed_listings(cur, feed_id)
    conn.commit()
    cur.close()
    if action in ("approve", "import"):
        ok, message = await run_in_threadpool(sync_feed, conn, feed_id)
    else:
        _photo_hosts_cache["at"] = 0
        ok, message = True, ("Rejected" if action == "reject" else "Disabled") + "; its listings are hidden"
    conn.close()
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------------------
# Daily run (GitHub Actions)
# ---------------------------------------------------------------------------
def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    ensure_feed_schema(cur)
    conn.commit()
    cur.execute("SELECT f.id, f.url FROM agent_feeds f JOIN agent_profiles p ON p.user_id = f.user_id "
                "WHERE f.status = 'approved' ORDER BY f.id")
    feeds = cur.fetchall()
    cur.close()
    print(f"{len(feeds)} approved feed(s)")
    failed = 0
    for feed_id, url in feeds:
        try:
            ok, message = sync_feed(conn, feed_id)
        except psycopg2.Error as e:
            conn.rollback()
            ok, message = False, f"database error: {e}"
        failed += not ok
        print(f"[{'ok' if ok else 'FAILED'}] feed {feed_id} {url}: {message}")
    conn.close()
    return 1 if failed else 0  # a failed run makes GitHub email the repo owner


if __name__ == "__main__":
    sys.exit(main())
