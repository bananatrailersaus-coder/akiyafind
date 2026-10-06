"""
alerts.py — AkiyaFind saved-search email alerts.

Shared by server.py (schema + filter SQL) and run daily after the crawler:

    export DATABASE_URL="postgresql://..."
    export RESEND_API_KEY="re_..."
    export ALERT_FROM_EMAIL="AkiyaFind <alerts@akiyafind.com>"
    python3 alerts.py              # send alerts for listings first seen since the last run
    python3 alerts.py --dry-run    # print what would be sent, change nothing
"""

import argparse
import html
import os
import time
from urllib.parse import urlencode

import psycopg2
import requests

BASE_URL         = os.environ.get("BASE_URL") or "https://akiyafind.com"
RESEND_API_KEY   = os.environ.get("RESEND_API_KEY")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL") or "AkiyaFind <alerts@akiyafind.com>"

# Saved searches allowed per tier. Free gets one so demand can be measured
# before payments are live.
ALERT_LIMITS = {"free": 1, "searcher": 10, "buyer": 10}

MAX_LISTINGS_PER_EMAIL = 20


def ensure_alert_schema(cur):
    # created_at marks when the crawler first saw a listing; the upsert never
    # overwrites it, so it is what "new listing" means for alerts.
    cur.execute("ALTER TABLE IF EXISTS listings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_searches (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            q               TEXT NOT NULL DEFAULT '',
            prefecture      TEXT NOT NULL DEFAULT '',
            min_price       BIGINT NOT NULL DEFAULT 0,
            max_price       BIGINT NOT NULL DEFAULT 0,
            unsub_token     TEXT UNIQUE NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            last_checked_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def listing_filter_sql(q="", prefecture="", min_price=0, max_price=0):
    """WHERE-clause fragment shared by /api/search and alerts so both match the same listings."""
    sql, params = "", []
    if q:
        sql += " AND (LOWER(city) LIKE %s OR LOWER(prefecture) LIKE %s OR LOWER(title_en) LIKE %s)"
        params += [f"%{q.lower()}%"] * 3
    if prefecture:
        sql += " AND LOWER(prefecture) = %s"
        params.append(prefecture.lower())
    if min_price:
        sql += " AND price_jpy >= %s"
        params.append(min_price)
    if max_price:
        sql += " AND price_jpy <= %s"
        params.append(max_price)
    return sql, params


def describe_search(q, prefecture, min_price, max_price):
    parts = []
    if q:
        parts.append(f'"{q}"')
    parts.append(prefecture or "all prefectures")
    if min_price and max_price:
        parts.append(f"¥{min_price:,}–¥{max_price:,}")
    elif min_price:
        parts.append(f"¥{min_price:,}+")
    elif max_price:
        parts.append(f"under ¥{max_price:,}")
    return ", ".join(parts)


def search_url(q, prefecture, min_price, max_price):
    params = {k: v for k, v in
              {"q": q, "prefecture": prefecture, "min_price": min_price, "max_price": max_price}.items() if v}
    return f"{BASE_URL}/search" + (f"?{urlencode(params)}" if params else "")


def render_email(search, listings, total):
    q, prefecture, min_price, max_price = search["q"], search["prefecture"], search["min_price"], search["max_price"]
    desc = describe_search(q, prefecture, min_price, max_price)
    unsub = f"{BASE_URL}/alerts/unsubscribe?token={search['unsub_token']}"
    view_all = search_url(q, prefecture, min_price, max_price)

    rows_html, rows_text = [], []
    for title, pref, city, price, size, url in listings:
        price_str = "¥0 — free transfer" if price == 0 else (f"¥{price:,}" if price else "Price on request")
        place = ", ".join(p for p in (city, pref) if p)
        size_str = f" · {size:g} m²" if size else ""
        rows_html.append(
            f'<tr><td style="padding:12px 0;border-bottom:1px solid #eee;">'
            f'<a href="{html.escape(url)}" style="color:#c94a2a;font-weight:600;text-decoration:none;">'
            f'{html.escape(title or "Vacant property")}</a><br>'
            f'<span style="color:#555;">{html.escape(place)}</span><br>'
            f'<span style="color:#1a1208;">{html.escape(price_str)}{html.escape(size_str)}</span>'
            f'</td></tr>'
        )
        rows_text.append(f"- {title or 'Vacant property'} — {place} — {price_str}{size_str}\n  {url}")

    more = total - len(listings)
    more_html = f'<p><a href="{html.escape(view_all)}">See all {total} new matches →</a></p>' if more > 0 else ""
    more_text = f"\nSee all {total} new matches: {view_all}\n" if more > 0 else ""
    noun = "listing" if total == 1 else "listings"

    subject = f"{total} new akiya {noun}: {desc}"
    body_html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1208;">
<h2 style="font-weight:600;">{total} new {noun} for {html.escape(desc)}</h2>
<table style="width:100%;border-collapse:collapse;">{''.join(rows_html)}</table>
{more_html}
<p style="color:#888;font-size:12px;margin-top:32px;">You're receiving this because you saved this search on AkiyaFind.
<a href="{html.escape(unsub)}" style="color:#888;">Unsubscribe from this alert</a>.</p>
</div>"""
    body_text = (f"{total} new {noun} for {desc}\n\n" + "\n".join(rows_text) + "\n" + more_text +
                 f"\nUnsubscribe from this alert: {unsub}\n")
    return subject, body_html, body_text, unsub


def send_email(to, subject, body_html, body_text, unsub_url):
    payload = {
        "from": ALERT_FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "html": body_html,
        "text": body_text,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    for attempt in range(3):
        r = requests.post("https://api.resend.com/emails", json=payload, timeout=20,
                          headers={"Authorization": f"Bearer {RESEND_API_KEY}"})
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return
    r.raise_for_status()


def run(dry_run=False):
    if not dry_run and not RESEND_API_KEY:
        print("RESEND_API_KEY not set — skipping alerts (watermarks unchanged).")
        return

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    ensure_alert_schema(cur)
    conn.commit()

    cur.execute("SELECT NOW()")
    run_started = cur.fetchone()[0]

    cur.execute("""
        SELECT s.id, s.q, s.prefecture, s.min_price, s.max_price, s.unsub_token, s.last_checked_at, u.email
        FROM saved_searches s JOIN users u ON u.id = s.user_id
        ORDER BY s.id
    """)
    searches = cur.fetchall()
    sent = failed = 0

    for sid, q, prefecture, min_price, max_price, token, last_checked, email in searches:
        search = {"q": q, "prefecture": prefecture, "min_price": min_price,
                  "max_price": max_price, "unsub_token": token}
        where, params = listing_filter_sql(q, prefecture, min_price, max_price)
        cur.execute(
            "SELECT COUNT(*) FROM listings WHERE created_at > %s AND created_at <= %s" + where,
            [last_checked, run_started] + params)
        total = cur.fetchone()[0]

        if total:
            cur.execute(
                "SELECT title_en, prefecture, city, price_jpy, size_m2, source_url FROM listings "
                "WHERE created_at > %s AND created_at <= %s" + where +
                " ORDER BY created_at DESC LIMIT %s",
                [last_checked, run_started] + params + [MAX_LISTINGS_PER_EMAIL])
            subject, body_html, body_text, unsub = render_email(search, cur.fetchall(), total)
            if dry_run:
                print(f"[dry-run] to={email} subject={subject!r}")
                continue
            try:
                send_email(email, subject, body_html, body_text, unsub)
                sent += 1
            except Exception as e:
                # Leave the watermark alone so these listings are retried next run.
                print(f"Failed to send alert {sid} to {email}: {e}")
                failed += 1
                continue
            time.sleep(0.6)  # stay under Resend's default rate limit

        if not dry_run:
            cur.execute("UPDATE saved_searches SET last_checked_at=%s WHERE id=%s", (run_started, sid))
            conn.commit()

    cur.close()
    conn.close()
    print(f"Alerts: {len(searches)} saved searches, {sent} emails sent, {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    run(dry_run=parser.parse_args().dry_run)
