"""
server.py — AkiyaFind (with Google OAuth + Stripe payments)
Replace your existing server.py with this file.
"""

import os, secrets, hashlib, hmac
import psycopg2
import stripe
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

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

STRIPE_SEARCHER_PRICE = os.environ.get("STRIPE_SEARCHER_PRICE", "price_1Tbf5pDEatFf7dD17ECzSvCF")
STRIPE_BUYER_PRICE    = os.environ.get("STRIPE_BUYER_PRICE",    "price_1Tbf6yDEatFf7dD1U6yAB8cz")

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
        FROM listings ORDER BY created_at DESC LIMIT 20
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
    return JSONResponse({"user": user})

# ---------------------------------------------------------------------------
# Stripe checkout
# ---------------------------------------------------------------------------
@app.post("/api/checkout")
async def create_checkout(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    body = await request.json()
    plan = body.get("plan")  # "searcher" or "buyer"
    price_id = STRIPE_SEARCHER_PRICE if plan == "searcher" else STRIPE_BUYER_PRICE

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

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(
                __import__("json").loads(payload), stripe.api_key
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        sub = event["data"]["object"]
        customer_id = sub["customer"]
        status = sub["status"]
        # Get price to determine tier
        price_id = sub["items"]["data"][0]["price"]["id"]
        if status == "active":
            tier = "buyer" if price_id == STRIPE_BUYER_PRICE else "searcher"
        else:
            tier = "free"
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
    query = """SELECT title_en, prefecture, city, price_jpy, size_m2, source_name, source_url,
                      is_free, lat, lng, image_url
               FROM listings WHERE 1=1"""
    params = []
    if q:
        query += " AND (LOWER(city) LIKE %s OR LOWER(prefecture) LIKE %s OR LOWER(title_en) LIKE %s)"
        params += [f"%{q.lower()}%"] * 3
    if prefecture:
        query += " AND LOWER(prefecture) = %s"
        params.append(prefecture.lower())
    if min_price:
        query += " AND price_jpy >= %s"
        params.append(min_price)
    if max_price:
        query += " AND price_jpy <= %s"
        params.append(max_price)
    query += " ORDER BY created_at DESC LIMIT 2000"
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
import stripe
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi import Request, HTTPException

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

PRICE_IDS = {
    "searcher": "price_1Tbf5pDEatFf7dD17ECzSvCF",
    "buyer":    "price_1Tbf6yDEatFf7dD1U6yAB8cz",
}

@app.get("/subscribe/{plan}")
async def subscribe(plan: str, request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login")
    if plan not in PRICE_IDS:
        raise HTTPException(400, "Invalid plan")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
        success_url="https://akiyafind.com/account?upgraded=1",
        cancel_url="https://akiyafind.com/pricing",
        customer_email=user["email"],
        metadata={"user_email": user["email"], "plan": plan},
    )
    return RedirectResponse(session.url)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]

    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except Exception as e:
        raise HTTPException(400, str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session["metadata"]["user_email"]
        plan  = session["metadata"]["plan"]
        sub_id = session.get("subscription")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET tier=%s, stripe_subscription_id=%s WHERE email=%s",
            (plan, sub_id, email)
        )
        conn.commit()
        conn.close()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub["id"]

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET tier='free', stripe_subscription_id=NULL WHERE stripe_subscription_id=%s",
            (sub_id,)
        )
        conn.commit()
        conn.close()

    return JSONResponse({"status": "ok"})
@app.get("/api/me")
async def get_me(request: Request):
    user = request.session.get("user")
    if user:
        return user
    return {}
@app.get("/api/img")
async def image_proxy(url: str, request: Request):
    from fastapi.responses import Response
    import httpx
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(url, headers={
                "Referer": "https://www.akiya-athome.jp/",
                "User-Agent": "Mozilla/5.0"
            }, follow_redirects=True, timeout=10)
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
    except Exception as e:
        return Response(content=b'', status_code=404)