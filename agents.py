"""
agents.py — AkiyaFind listings submitted directly by real estate agents.

Agents sign in with Google, add their company and Japanese real estate
licence, and submit listings with photos. An admin (ADMIN_EMAILS) reviews
each one; approved listings are mirrored into the main `listings` table, so
search, the map and saved-search alerts include them. Buyers contact the
agent through the listing page; enquiries are stored and emailed to the agent.
"""

import io
import os
import re
import time
import warnings
from collections import defaultdict, deque
from email.utils import parseaddr
from urllib.parse import quote

import psycopg2
import requests
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from pages import render_page

router = APIRouter()

BASE_URL         = os.environ.get("BASE_URL") or "https://akiyafind.com"
RESEND_API_KEY   = os.environ.get("RESEND_API_KEY")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL") or "AkiyaFind <alerts@akiyafind.com>"
ADMIN_EMAILS     = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

PREFECTURES = (
    "Hokkaido", "Aomori", "Iwate", "Miyagi", "Akita", "Yamagata", "Fukushima", "Ibaraki", "Tochigi",
    "Gunma", "Saitama", "Chiba", "Tokyo", "Kanagawa", "Niigata", "Toyama", "Ishikawa", "Fukui",
    "Yamanashi", "Nagano", "Gifu", "Shizuoka", "Aichi", "Mie", "Shiga", "Kyoto", "Osaka", "Hyogo",
    "Nara", "Wakayama", "Tottori", "Shimane", "Okayama", "Hiroshima", "Yamaguchi", "Tokushima",
    "Kagawa", "Ehime", "Kochi", "Fukuoka", "Saga", "Nagasaki", "Kumamoto", "Oita", "Miyazaki",
    "Kagoshima", "Okinawa",
)

MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 12 * 1024 * 1024
PHOTO_MAX_SIDE = 1600
Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs

ENQUIRY_LIMIT_PER_HOUR = 5
_enquiry_times = defaultdict(deque)  # ip -> recent enquiry timestamps (per process)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_agent_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_profiles (
            user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            company      TEXT NOT NULL,
            contact_name TEXT NOT NULL DEFAULT '',
            email        TEXT NOT NULL,
            phone        TEXT NOT NULL DEFAULT '',
            website      TEXT NOT NULL DEFAULT '',
            license_no   TEXT NOT NULL,
            languages    TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_listings (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | withdrawn
            title       TEXT NOT NULL,
            prefecture  TEXT NOT NULL,
            city        TEXT NOT NULL DEFAULT '',
            price_jpy   BIGINT,
            land_m2     DOUBLE PRECISION,
            building_m2 DOUBLE PRECISION,
            year_built  INTEGER,
            layout      TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            listing_url TEXT NOT NULL DEFAULT '',
            review_note TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_photos (
            id           SERIAL PRIMARY KEY,
            listing_id   INTEGER NOT NULL REFERENCES agent_listings(id) ON DELETE CASCADE,
            position     INTEGER NOT NULL DEFAULT 0,
            content_type TEXT NOT NULL,
            data         BYTEA NOT NULL,
            width        INTEGER,
            height       INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS enquiries (
            id         SERIAL PRIMARY KEY,
            listing_id INTEGER NOT NULL REFERENCES agent_listings(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            email      TEXT NOT NULL,
            message    TEXT NOT NULL,
            emailed    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------
def session_user(request: Request):
    """The signed-in user's DB row as a dict, or None."""
    u = request.session.get("user")
    if not u:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, email, name FROM users WHERE email=%s", (u["email"],))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {"id": row[0], "email": row[1], "name": row[2]} if row else None


def require_user(request: Request):
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in")
    return user


def is_admin(user):
    return bool(user) and user["email"].lower() in ADMIN_EMAILS


def require_admin(request: Request):
    user = session_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=404)  # don't advertise that the admin area exists
    return user


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def text(value, limit, required=False, label="Field"):
    v = str(value or "").strip()
    if required and not v:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(v) > limit:
        raise HTTPException(status_code=400, detail=f"{label} is too long (max {limit} characters)")
    return v


def url_or_blank(value, label):
    v = text(value, 500, label=label)
    if v and not re.match(r"^https?://[^\s]+$", v, re.I):
        raise HTTPException(status_code=400, detail=f"{label} must start with http:// or https://")
    return v


def number(value, label, kind=float, lo=0, hi=1e12):
    v = str(value or "").strip().replace(",", "")
    if not v:
        return None
    try:
        n = kind(v)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be a number")
    if not (lo <= n <= hi):
        raise HTTPException(status_code=400, detail=f"{label} is out of range")
    return n


def process_photo(raw):
    """Re-encode an upload as a JPEG, max 1600px. Re-encoding drops all metadata, including GPS."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            Image.open(io.BytesIO(raw)).verify()
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)  # keep the photo the right way up
            img = img.convert("RGB")
            img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=82, optimize=True)
    except Exception:
        raise HTTPException(status_code=400, detail="One of the photos isn't a readable image")
    return out.getvalue(), img.size


# ---------------------------------------------------------------------------
# Publishing into the main listings table
# ---------------------------------------------------------------------------
def listing_page_url(listing_id):
    return f"{BASE_URL}/listing/{listing_id}"


def publish(cur, listing_id):
    cur.execute("""
        SELECT l.title, l.prefecture, l.city, l.price_jpy, COALESCE(l.building_m2, l.land_m2), p.company,
               (SELECT id FROM agent_photos WHERE listing_id = l.id ORDER BY position, id LIMIT 1), l.photo_urls[1],
               l.feed_removed_at
        FROM agent_listings l JOIN agent_profiles p ON p.user_id = l.user_id
        WHERE l.id = %s
    """, (listing_id,))
    title, pref, city, price, size, company, photo_id, feed_photo, feed_removed_at = cur.fetchone()
    if feed_removed_at is not None:  # no longer in the agent's feed: approving it mustn't bring it back
        unpublish(cur, listing_id)
        return
    # Uploaded photos are served by us; feed photos (agent's server) go through /api/img like source-site photos.
    image = f"/agent-photos/{photo_id}" if photo_id else (feed_photo or "")
    cur.execute("""
        INSERT INTO listings (source_url, prefecture, city, title_en, title_jp, price_jpy, size_m2,
                              source_name, is_free, image_url, agent_listing_id, last_seen_at, delisted_at)
        VALUES (%s, %s, %s, %s, '', %s, %s, %s, %s, %s, %s, NOW(), NULL)
        ON CONFLICT (source_url) DO UPDATE SET
            prefecture = EXCLUDED.prefecture, city = EXCLUDED.city, title_en = EXCLUDED.title_en,
            price_jpy = EXCLUDED.price_jpy, size_m2 = EXCLUDED.size_m2, source_name = EXCLUDED.source_name,
            is_free = EXCLUDED.is_free, image_url = EXCLUDED.image_url,
            agent_listing_id = EXCLUDED.agent_listing_id, last_seen_at = NOW(), delisted_at = NULL
    """, (listing_page_url(listing_id), pref, city, title, price, size, company, price == 0, image, listing_id))


def unpublish(cur, listing_id):
    cur.execute("UPDATE listings SET delisted_at = NOW() WHERE agent_listing_id = %s AND delisted_at IS NULL",
                (listing_id,))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@router.get("/agents", response_class=HTMLResponse)
def agents_page():
    return render_page("agents.html")


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    require_admin(request)
    return render_page("admin.html")


@router.get("/listing/{listing_id}", response_class=HTMLResponse)
def listing_page(listing_id: int):
    return render_page("listing.html")


# ---------------------------------------------------------------------------
# Agent API
# ---------------------------------------------------------------------------
LISTING_COLS = """l.id, l.status, l.title, l.prefecture, l.city, l.price_jpy, l.land_m2, l.building_m2,
                  l.year_built, l.layout, l.description, l.listing_url, l.review_note, l.created_at,
                  l.photo_urls, l.feed_id, l.feed_removed_at"""
LISTING_KEYS = ["id", "status", "title", "prefecture", "city", "price_jpy", "land_m2", "building_m2",
                "year_built", "layout", "description", "listing_url", "review_note", "created_at",
                "feed_photo_urls", "feed_id", "feed_removed_at"]


def image_proxy_path(url):
    return "/api/img?url=" + quote(url, safe="")


def listing_dict(row, photo_ids):
    d = dict(zip(LISTING_KEYS, row))
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    feed_photos = d.pop("feed_photo_urls") or []
    d["photo_urls"] = [f"/agent-photos/{p}" for p in photo_ids] or [image_proxy_path(u) for u in feed_photos]
    d["from_feed"] = d.pop("feed_id") is not None
    d["in_feed"] = d.pop("feed_removed_at") is None  # False once it dropped out of the feed
    d["page_url"] = f"/listing/{d['id']}"
    return d


def photo_ids_for(cur, listing_ids):
    out = defaultdict(list)
    if listing_ids:
        cur.execute("SELECT listing_id, id FROM agent_photos WHERE listing_id = ANY(%s) ORDER BY position, id",
                    (list(listing_ids),))
        for lid, pid in cur.fetchall():
            out[lid].append(pid)
    return out


PROFILE_KEYS = ["company", "contact_name", "email", "phone", "website", "license_no", "languages"]


@router.get("/api/agent/me")
def agent_me(request: Request):
    user = session_user(request)
    if not user:
        return {"user": None, "prefectures": PREFECTURES}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(PROFILE_KEYS)} FROM agent_profiles WHERE user_id=%s", (user["id"],))
    row = cur.fetchone()
    profile = dict(zip(PROFILE_KEYS, row)) if row else None
    cur.execute(f"SELECT {LISTING_COLS} FROM agent_listings l WHERE l.user_id=%s ORDER BY l.id DESC", (user["id"],))
    rows = cur.fetchall()
    photos = photo_ids_for(cur, [r[0] for r in rows])
    cur.execute("""SELECT e.id, e.listing_id, l.title, e.name, e.email, e.message, e.created_at
                   FROM enquiries e JOIN agent_listings l ON l.id = e.listing_id
                   WHERE l.user_id=%s ORDER BY e.id DESC LIMIT 100""", (user["id"],))
    enquiries = [{"id": r[0], "listing_id": r[1], "listing_title": r[2], "name": r[3], "email": r[4],
                  "message": r[5], "created_at": r[6].isoformat()} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"user": {"email": user["email"], "name": user["name"]}, "is_admin": is_admin(user),
            "profile": profile, "listings": [listing_dict(r, photos[r[0]]) for r in rows],
            "enquiries": enquiries, "prefectures": PREFECTURES}


@router.put("/api/agent/profile")
async def save_profile(request: Request):
    user = require_user(request)
    body = await request.json()
    p = {
        "company":      text(body.get("company"), 200, True, "Company name"),
        "contact_name": text(body.get("contact_name"), 200, label="Contact name"),
        "email":        text(body.get("email"), 200, True, "Email"),
        "phone":        text(body.get("phone"), 50, label="Phone"),
        "website":      url_or_blank(body.get("website"), "Website"),
        "license_no":   text(body.get("license_no"), 100, True, "Licence number"),
        "languages":    text(body.get("languages"), 200, label="Languages"),
    }
    if not EMAIL_RE.match(p["email"]):
        raise HTTPException(status_code=400, detail="Email doesn't look right")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO agent_profiles (user_id, {', '.join(PROFILE_KEYS)})
        VALUES (%s, {', '.join(['%s'] * len(PROFILE_KEYS))})
        ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{k} = EXCLUDED.{k}' for k in PROFILE_KEYS)}, updated_at = NOW()
    """, [user["id"]] + [p[k] for k in PROFILE_KEYS])
    # Company name shows on published listings, so keep them in step.
    cur.execute("UPDATE listings SET source_name=%s WHERE agent_listing_id IN "
                "(SELECT id FROM agent_listings WHERE user_id=%s)", (p["company"], user["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return {"profile": p}


@router.post("/api/agent/listings")
async def create_listing(
    request: Request,
    title: str = Form(...), prefecture: str = Form(...), city: str = Form(""),
    price_jpy: str = Form(""), land_m2: str = Form(""), building_m2: str = Form(""),
    year_built: str = Form(""), layout: str = Form(""), description: str = Form(""),
    listing_url: str = Form(""), photos: list[UploadFile] = File(default=[]),
):
    user = require_user(request)
    if prefecture not in PREFECTURES:
        raise HTTPException(status_code=400, detail="Choose a prefecture")
    fields = {
        "title":       text(title, 150, True, "Title"),
        "prefecture":  prefecture,
        "city":        text(city, 100, label="Town / city"),
        "price_jpy":   number(price_jpy, "Price", int, 0, 100_000_000_000),
        "land_m2":     number(land_m2, "Land area", float, 0, 10_000_000),
        "building_m2": number(building_m2, "Building area", float, 0, 1_000_000),
        "year_built":  number(year_built, "Year built", int, 1600, 2100),
        "layout":      text(layout, 50, label="Layout"),
        "description": text(description, 5000, label="Description"),
        "listing_url": url_or_blank(listing_url, "Your listing page"),
    }
    photos = [p for p in photos if p.filename]
    if not photos:
        raise HTTPException(status_code=400, detail="Add at least one photo")
    if len(photos) > MAX_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Up to {MAX_PHOTOS} photos per listing")
    processed = []
    for p in photos:
        raw = await p.read(MAX_PHOTO_BYTES + 1)
        if len(raw) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=400, detail="Each photo must be under 12 MB")
        processed.append(process_photo(raw))

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM agent_profiles WHERE user_id=%s", (user["id"],))
        if not cur.fetchone():
            raise HTTPException(status_code=400, detail="Add your company details and licence number first")
        cols = list(fields)
        cur.execute(f"INSERT INTO agent_listings (user_id, {', '.join(cols)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(cols))}) RETURNING id",
                    [user["id"]] + [fields[c] for c in cols])
        listing_id = cur.fetchone()[0]
        for pos, (data, (w, h)) in enumerate(processed):
            cur.execute("""INSERT INTO agent_photos (listing_id, position, content_type, data, width, height)
                           VALUES (%s, %s, 'image/jpeg', %s, %s, %s)""",
                        (listing_id, pos, psycopg2.Binary(data), w, h))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"id": listing_id, "status": "pending"}


@router.post("/api/agent/listings/{listing_id}/withdraw")
def withdraw_listing(listing_id: int, request: Request):
    user = require_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE agent_listings SET status='withdrawn', updated_at=NOW() "
                "WHERE id=%s AND user_id=%s AND status <> 'withdrawn'", (listing_id, user["id"]))
    found = cur.rowcount
    if found:
        unpublish(cur, listing_id)
    conn.commit()
    cur.close()
    conn.close()
    if not found:
        raise HTTPException(status_code=404, detail="Listing not found")
    return {"status": "withdrawn"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def can_preview(user, owner_id):
    return bool(user) and (user["id"] == owner_id or is_admin(user))


@router.get("/api/listing/{listing_id}")
def public_listing(listing_id: int, request: Request):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT {LISTING_COLS}, l.user_id FROM agent_listings l WHERE l.id=%s", (listing_id,))
    row = cur.fetchone()
    visible = row and row[1] == "approved" and row[LISTING_KEYS.index("feed_removed_at")] is None
    if not row or (not visible and not can_preview(session_user(request), row[-1])):
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Listing not found")
    photos = photo_ids_for(cur, [listing_id])[listing_id]
    cur.execute("SELECT company, website, license_no, languages FROM agent_profiles WHERE user_id=%s", (row[-1],))
    company, website, license_no, languages = cur.fetchone()
    cur.close()
    conn.close()
    d = listing_dict(row[:-1], photos)
    d.pop("review_note")
    d["agent"] = {"company": company, "website": website, "license_no": license_no, "languages": languages}
    return d


@router.get("/agent-photos/{photo_id}")
def agent_photo(photo_id: int, request: Request):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT p.data, p.content_type, l.status, l.user_id FROM agent_photos p
                   JOIN agent_listings l ON l.id = p.listing_id WHERE p.id=%s""", (photo_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404)
    data, ctype, status, owner = row
    if status != "approved":
        if not can_preview(session_user(request), owner):
            raise HTTPException(status_code=404)
        return Response(content=bytes(data), media_type=ctype, headers={"Cache-Control": "private, no-store"})
    return Response(content=bytes(data), media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


def client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def personal_from(name):
    """'Jane Buyer via AkiyaFind <alerts@…>'. Mail that reads as from a person is less likely to be
    filed under Gmail's Updates tab, where agents might miss it. Quotes, brackets and commas are dropped
    so the name can't break the address."""
    display, addr = parseaddr(ALERT_FROM_EMAIL)
    clean = re.sub(r"[^\w .'-]", "", name).strip()[:60]
    return f'"{clean} via {display or "AkiyaFind"}" <{addr}>' if clean and addr else ALERT_FROM_EMAIL


def email_agent(to, reply_to, subject, text_body, sender=None):
    if not RESEND_API_KEY:
        return False
    for from_ in dict.fromkeys([sender or ALERT_FROM_EMAIL, ALERT_FROM_EMAIL]):
        try:
            r = requests.post("https://api.resend.com/emails", timeout=20,
                              headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                              json={"from": from_, "to": [to], "reply_to": reply_to,
                                    "subject": subject, "text": text_body})
        except requests.RequestException:
            return False
        if r.status_code < 300:
            return True
        if r.status_code not in (400, 422):
            return False  # only a rejected From is worth retrying with the plain sender
    return False


@router.post("/api/listing/{listing_id}/enquire")
async def enquire(listing_id: int, request: Request):
    body = await request.json()
    if str(body.get("website") or "").strip():  # honeypot: humans never see this field
        return {"status": "sent"}
    name = text(body.get("name"), 100, True, "Name")
    email = text(body.get("email"), 200, True, "Email")
    message = text(body.get("message"), 3000, True, "Message")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Email doesn't look right")

    ip, now = client_ip(request), time.time()
    recent = _enquiry_times[ip]
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= ENQUIRY_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many enquiries — please try again later")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT l.title, p.email FROM agent_listings l JOIN agent_profiles p ON p.user_id = l.user_id
                   WHERE l.id=%s AND l.status='approved' AND l.feed_removed_at IS NULL""", (listing_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Listing not found")
    title, agent_email = row
    cur.execute("INSERT INTO enquiries (listing_id, name, email, message) VALUES (%s,%s,%s,%s) RETURNING id",
                (listing_id, name, email, message))
    enquiry_id = cur.fetchone()[0]
    conn.commit()
    recent.append(now)

    # The buyer's message comes first and there's a single link: it should read like a person's email.
    sent = await run_in_threadpool(
        email_agent, agent_email, email, f"Enquiry about {title}",
        f"{message}\n\n{name} <{email}>\n\n"
        f"--\nSent through AkiyaFind about your listing \"{title}\":\n{listing_page_url(listing_id)}\n"
        f"Reply to this email to answer {name} directly.\n",
        personal_from(name))
    if sent:
        cur.execute("UPDATE enquiries SET emailed=TRUE WHERE id=%s", (enquiry_id,))
        conn.commit()
    cur.close()
    conn.close()
    return {"status": "sent"}


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------
@router.get("/api/admin/listings")
def admin_listings(request: Request, status: str = "pending"):
    require_admin(request)
    if status not in ("pending", "approved", "rejected", "withdrawn"):
        raise HTTPException(status_code=400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"""SELECT {LISTING_COLS}, p.company, p.license_no, p.email, p.phone, p.website, p.contact_name
                    FROM agent_listings l JOIN agent_profiles p ON p.user_id = l.user_id
                    WHERE l.status=%s ORDER BY l.id DESC LIMIT 200""", (status,))
    rows = cur.fetchall()
    photos = photo_ids_for(cur, [r[0] for r in rows])
    out = []
    for r in rows:
        d = listing_dict(r[:len(LISTING_KEYS)], photos[r[0]])
        d["agent"] = dict(zip(["company", "license_no", "email", "phone", "website", "contact_name"], r[len(LISTING_KEYS):]))
        out.append(d)
    cur.execute("""SELECT e.id, e.listing_id, l.title, e.name, e.email, e.message, e.emailed, e.created_at
                   FROM enquiries e JOIN agent_listings l ON l.id = e.listing_id ORDER BY e.id DESC LIMIT 50""")
    enquiries = [{"id": r[0], "listing_id": r[1], "listing_title": r[2], "name": r[3], "email": r[4],
                  "message": r[5], "emailed": r[6], "created_at": r[7].isoformat()} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"listings": out, "enquiries": enquiries}


@router.post("/api/admin/listings/{listing_id}/{action}")
async def admin_review(listing_id: int, action: str, request: Request):
    require_admin(request)
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=404)
    note = ""
    if action == "reject":
        body = await request.json()
        note = text(body.get("note"), 1000, label="Note")
    conn = get_db()
    cur = conn.cursor()
    status = "approved" if action == "approve" else "rejected"
    cur.execute("UPDATE agent_listings SET status=%s, review_note=%s, reviewed_at=NOW(), updated_at=NOW() "
                "WHERE id=%s AND status <> 'withdrawn'", (status, note, listing_id))
    if not cur.rowcount:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Listing not found or withdrawn")
    if status == "approved":
        publish(cur, listing_id)
    else:
        unpublish(cur, listing_id)
    conn.commit()
    cur.close()
    conn.close()
    return {"status": status}
