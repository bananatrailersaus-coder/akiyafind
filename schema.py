"""
schema.py — AkiyaFind listings columns shared by the server, alerts and feed import.

Run from each entry point because any of them may hit the database first
after a deploy. Every statement is idempotent.
"""

# column -> type. created_at: when a listing was first published (never
# overwritten) — "new" for alerts. last_seen_at: last time it was published or
# re-imported. delisted_at: set when it's taken down. agent_listing_id: the
# agent listing (agents.py / feeds.py) it mirrors. Only rows with an
# agent_listing_id are ever shown (alerts.PUBLISHED_SQL).
LISTING_COLUMNS = {
    "created_at":       "TIMESTAMPTZ DEFAULT NOW()",
    "last_seen_at":     "TIMESTAMPTZ DEFAULT NOW()",
    "delisted_at":      "TIMESTAMPTZ",
    "agent_listing_id": "INTEGER",
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
