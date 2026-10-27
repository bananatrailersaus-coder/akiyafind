"""
schema.py — AkiyaFind listings columns shared by the crawler, server and alerts.

Run from each entry point because any of them may hit the database first
after a deploy. Every statement is idempotent.
"""

# column -> type. created_at: when the crawler first saw a listing (never
# overwritten) — "new" for alerts. last_seen_at: last crawl that found it on the
# source site. delisted_at: set when a complete crawl of its prefecture no
# longer finds it.
LISTING_COLUMNS = {
    "created_at":   "TIMESTAMPTZ DEFAULT NOW()",
    "last_seen_at": "TIMESTAMPTZ DEFAULT NOW()",
    "delisted_at":  "TIMESTAMPTZ",
}


def ensure_listing_columns(cur):
    # ALTER TABLE takes an exclusive lock even when the column already exists,
    # and every query on listings queues behind it, so only ALTER when needed.
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'listings'""")
    existing = {r[0] for r in cur.fetchall()}
    missing = [c for c in LISTING_COLUMNS if c not in existing]
    if existing and missing:
        # Fail fast instead of freezing the site if a long query holds the table.
        cur.execute("SET LOCAL lock_timeout = '10s'")
        for col in missing:
            cur.execute(f"ALTER TABLE listings ADD COLUMN IF NOT EXISTS {col} {LISTING_COLUMNS[col]}")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crawl_log (
            id          SERIAL PRIMARY KEY,
            prefecture  TEXT NOT NULL,
            started_at  TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            complete    BOOLEAN NOT NULL,
            seen        INTEGER NOT NULL,
            delisted    INTEGER NOT NULL DEFAULT 0
        )
    """)
