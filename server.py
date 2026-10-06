"""
server.py — AkiyaFind (with Google OAuth + Stripe payments)
Replace your existing server.py with this file.
"""

import os, secrets, hashlib, hmac, html
from urllib.parse import urlparse
import httpx
import psycopg2
import stripe
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from alerts import ensure_alert_schema, listing_filter_sql, ALERT_LIMITS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL   = os.environ.get("DATABASE_URL")
SECRET_KEY     = os.environ.get("SECRET_KEY", secrets.token_hex(32))

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

STRIPE_SEARCHER_PRICE = os.environ.get("STRIPE_SEARCHER_PRICE")

# Checkout is only offered once every piece Stripe needs is configured.
PAYMENTS_ENABLED = bool(STRIPE_SECRET_KEY and STRIPE_SEARCHER_PRICE and STRIPE_WEBHOOK_SECRET)

BASE_URL = os.environ.get("BASE_URL", "https://akiyafind.com")

stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def ensure_tables():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                SERIAL PRIMARY KEY,
            email             TEXT UNIQUE NOT NULL,
            google_id         TEXT UNIQUE,
            name              TEXT,
            picture           TEXT,
            tier              TEXT DEFAULT 'free',
            stripe_customer_id TEXT,
            stripe_sub_id     TEXT,
            created_at        TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    ensure_alert_schema(cur)
    conn.commit()
    cur.close()
    conn.close()

ensure_tables()

def get_user_by_email(email: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, email, name, picture, tier, stripe_customer_id FROM users WHERE email=%s", (email,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "picture": row[3], "tier": row[4], "stripe_customer_id": row[5]}

def upsert_user(google_id, email, name, picture):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (google_id, email, name, picture)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE SET
            google_id = EXCLUDED.google_id,
            name = EXCLUDED.name,
            picture = EXCLUDED.picture
        RETURNING id, email, name, picture, tier, stripe_customer_id
    """, (google_id, email, name, picture))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return {"id": row[0], "email": row[1], "name": row[2], "picture": row[3], "tier": row[4], "stripe_customer_id": row[5]}

def get_current_user(request: Request):
    return request.session.get("user")

# ---------------------------------------------------------------------------
# Existing DB helpers (keep same as before)
# ---------------------------------------------------------------------------
def get_listings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT title_en, prefecture, city, price_jpy, size_m2, source_name, source_url,
               is_free, lat, lng, image_url
        FROM listings ORDER BY RANDOM() LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    listings = []
    for row in rows:
        price_jpy = row[3] or 0
        price_aud = round(price_jpy * 0.0091) if price_jpy else 0
        listings.append({
            "title": row[0] or "Vacant Property",
            "prefecture": row[1],
            "city": row[2],
            "price_jpy": price_jpy,
            "price_aud": price_aud,
            "size_m2": row[4],
            "source_name": row[5],
            "source_url": row[6],
            "is_free": row[7],
            "lat": row[8],
            "lng": row[9],
         "image_url": row[10] or "",

        })
    return listings

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.get("/auth/login")
async def login(request: Request):
    redirect_uri = f"{BASE_URL}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get("userinfo")
        if not userinfo:
            raise HTTPException(status_code=400, detail="No user info")
        user = upsert_user(
            google_id=userinfo["sub"],
            email=userinfo["email"],
            name=userinfo.get("name", ""),
            picture=userinfo.get("picture", ""),
        )
        request.session["user"] = user
        return RedirectResponse(url="/account")
    except Exception as e:
        return RedirectResponse(url=f"/?error=auth_failed")

@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")

# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, "account.html")) as f:
        return f.read()

@app.get("/api/me")
async def api_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"user": None})
    # Re-read from the DB so a tier change from the Stripe webhook shows without re-login.
    user = get_user_by_email(user["email"]) or user
    request.session["user"] = user
    return JSONResponse({
        "user": user,
        "payments_enabled": PAYMENTS_ENABLED,
        "alert_limit": ALERT_LIMITS.get(user.get("tier"), ALERT_LIMITS["free"]),
    })

# ---------------------------------------------------------------------------
# Stripe checkout
# ---------------------------------------------------------------------------
@app.post("/api/checkout")
async def create_checkout(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    if not PAYMENTS_ENABLED:
        raise HTTPException(status_code=503, detail="Payments are not available yet")

    body = await request.json()
    plan = body.get("plan")
    if plan != "searcher":
        raise HTTPException(status_code=400, detail="Invalid plan")
    price_id = STRIPE_SEARCHER_PRICE

    # Create or retrieve Stripe customer
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=user["email"], name=user["name"])
        customer_id = customer.id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET stripe_customer_id=%s WHERE email=%s", (customer_id, user["email"]))
        conn.commit()
        cur.close()
        conn.close()
        user["stripe_customer_id"] = customer_id
        request.session["user"] = user

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}/account?success=1",
        cancel_url=f"{BASE_URL}/pricing",
        metadata={"user_email": user["email"], "plan": plan},
    )
    return JSONResponse({"url": session.url})

@app.post("/api/portal")
async def customer_portal(request: Request):
    user = get_current_user(request)
    if not user or not user.get("stripe_customer_id"):
        raise HTTPException(status_code=401)
    session = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=f"{BASE_URL}/account",
    )
    return JSONResponse({"url": session.url})

# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        sub = event["data"]["object"]
        customer_id = sub["customer"]
        status = sub["status"]
        tier = "searcher" if status == "active" else "free"
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET tier=%s, stripe_sub_id=%s WHERE stripe_customer_id=%s",
                    (tier, sub["id"], customer_id))
        conn.commit()
        cur.close()
        conn.close()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET tier='free', stripe_sub_id=NULL WHERE stripe_customer_id=%s",
                    (sub["customer"],))
        conn.commit()
        cur.close()
        conn.close()

    return JSONResponse({"status": "ok"})

# ---------------------------------------------------------------------------
# Existing routes (unchanged)
# ---------------------------------------------------------------------------
@app.get("/api/listings")
def api_listings():
    listings = get_listings()
    return {"listings": listings}

@app.get("/api/search")
def api_search(q: str = "", prefecture: str = "", min_price: int = 0, max_price: int = 0):
    conn = get_db()
    cur = conn.cursor()
    where, params = listing_filter_sql(q, prefecture, min_price, max_price)
    query = """SELECT title_en, prefecture, city, price_jpy, size_m2, source_name, source_url,
                      is_free, lat, lng, image_url
               FROM listings WHERE 1=1""" + where + " ORDER BY RANDOM() LIMIT 2000"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    listings = []
    for row in rows:
        price_jpy = row[3] or 0
        price_aud = round(price_jpy * 0.0091) if price_jpy else 0
        listings.append({
            "title": row[0] or "Vacant Property",
            "prefecture": row[1], "city": row[2],
            "price_jpy": price_jpy, "price_aud": price_aud,
            "size_m2": row[4], "source_name": row[5], "source_url": row[6],
            "is_free": row[7], "lat": row[8], "lng": row[9],
            "image_url": row[10] or "",
        })
    return {"listings": listings}

# ---------------------------------------------------------------------------
# Saved-search email alerts (sent daily by alerts.py after the crawl)
# ---------------------------------------------------------------------------
def _require_db_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    db_user = get_user_by_email(user["email"])
    if not db_user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return db_user

def _alert_row(row):
    return {"id": row[0], "q": row[1], "prefecture": row[2],
            "min_price": row[3], "max_price": row[4], "created_at": row[5].isoformat()}

@app.get("/api/alerts")
def list_alerts(request: Request):
    user = _require_db_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, q, prefecture, min_price, max_price, created_at
                   FROM saved_searches WHERE user_id=%s ORDER BY id""", (user["id"],))
    alerts = [_alert_row(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"alerts": alerts, "limit": ALERT_LIMITS.get(user["tier"], ALERT_LIMITS["free"])}

@app.post("/api/alerts")
async def create_alert(request: Request):
    user = _require_db_user(request)
    body = await request.json()
    try:
        q          = str(body.get("q") or "").strip()[:100]
        prefecture = str(body.get("prefecture") or "").strip()[:30]
        min_price  = max(0, int(body.get("min_price") or 0))
        max_price  = max(0, int(body.get("max_price") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid search")

    limit = ALERT_LIMITS.get(user["tier"], ALERT_LIMITS["free"])
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id FROM saved_searches WHERE user_id=%s AND q=%s AND prefecture=%s
                       AND min_price=%s AND max_price=%s""",
                    (user["id"], q, prefecture, min_price, max_price))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="You already have an alert for this search")
        cur.execute("SELECT COUNT(*) FROM saved_searches WHERE user_id=%s", (user["id"],))
        if cur.fetchone()[0] >= limit:
            raise HTTPException(status_code=403, detail=f"Your plan allows {limit} alert{'s' if limit != 1 else ''}")
        cur.execute("""INSERT INTO saved_searches (user_id, q, prefecture, min_price, max_price, unsub_token)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id, q, prefecture, min_price, max_price, created_at""",
                    (user["id"], q, prefecture, min_price, max_price, secrets.token_urlsafe(24)))
        alert = _alert_row(cur.fetchone())
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"alert": alert}

@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: int, request: Request):
    user = _require_db_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_searches WHERE id=%s AND user_id=%s", (alert_id, user["id"]))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "ok"}

UNSUB_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Unsubscribe — AkiyaFind</title></head>
<body style="font-family:Arial,sans-serif;background:#f9f6f1;color:#1a1a1a;padding:60px 16px;text-align:center;">
<h1 style="font-size:1.4rem;">{heading}</h1><p>{body}</p><p><a href="/" style="color:#c84b2f;">Back to AkiyaFind</a></p>
</body></html>"""

# GET only shows a confirm button: mail scanners prefetch links, so a GET must not unsubscribe.
@app.get("/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(token: str = ""):
    if not token:
        return UNSUB_PAGE.format(heading="Invalid unsubscribe link", body="Manage your alerts from your account page.")
    form = (f'<form method="post" action="/alerts/unsubscribe?token={html.escape(token, quote=True)}">'
            '<button type="submit" style="background:#c84b2f;color:white;border:none;padding:12px 28px;'
            'border-radius:6px;font-size:1rem;cursor:pointer;">Unsubscribe</button></form>')
    return UNSUB_PAGE.format(heading="Stop this email alert?", body=form)

# POST handles both the confirm button and RFC 8058 one-click unsubscribe from mail clients.
@app.post("/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str = ""):
    if token:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM saved_searches WHERE unsub_token=%s", (token,))
        conn.commit()
        cur.close()
        conn.close()
    return UNSUB_PAGE.format(heading="You're unsubscribed",
                             body="You won't get any more emails for that search.")

@app.get("/api/counts")
def api_counts():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT prefecture, COUNT(*) FROM listings GROUP BY prefecture")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"counts": {row[0]: row[1] for row in rows}}

@app.get("/search", response_class=HTMLResponse)
def search_page():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, "search.html")) as f:
        return f.read()

@app.get("/", response_class=HTMLResponse)
def homepage():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, "index.html")) as f:
        return f.read()

@app.get("/map", response_class=HTMLResponse)
def map_page():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, "map.html")) as f:
        return f.read()

@app.get("/pricing", response_class=HTMLResponse)
def pricing_page():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Try pricing.html, fall back to index.html#pricing
    try:
        with open(os.path.join(base_dir, "pricing.html")) as f:
            return f.read()
    except FileNotFoundError:
        with open(os.path.join(base_dir, "index.html")) as f:
            return f.read()
# Only proxy listing photos from the source site, never arbitrary URLs.
IMG_PROXY_HOSTS = ("akiya-athome.jp", "athome.jp")

def _is_allowed_img_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        any(host == h or host.endswith("." + h) for h in IMG_PROXY_HOSTS)
    )

@app.get("/api/img")
async def image_proxy(url: str, request: Request):
    if not _is_allowed_img_url(url):
        return Response(content=b'', status_code=400)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={
                "Referer": "https://www.akiya-athome.jp/",
                "User-Agent": "Mozilla/5.0"
            }, follow_redirects=False, timeout=10)
        content_type = r.headers.get("content-type", "")
        if r.status_code != 200 or not content_type.startswith("image/"):
            return Response(content=b'', status_code=404)
        return Response(content=r.content, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(content=b'', status_code=404)