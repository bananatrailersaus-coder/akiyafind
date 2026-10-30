# AkiyaFind — notes for Claude

English-language search site for akiya and affordable homes in Japan, for buyers overseas.
FastAPI + Postgres on **Railway**; every merge to `master` auto-deploys in ~30–90 s.
Domain `akiyafind.com` (DNS at Namecheap: ALIAS `@` → Railway, MX → ImprovMX forwarding).
`www.akiyafind.com` redirects to `akiyafind.com` (middleware in `server.py`).
This repo is **public**: keep business plans, outreach lists and personal details out of it.

## Code map
- `server.py` — routes, `/api/search` (with `total`) + `/api/listings` + `/api/counts`, `/api/img` image proxy (photo hosts of approved feeds only, redirects re-checked), auth (Google), Stripe checkout (hidden until all Stripe vars are set), www redirect.
- `agents.py` — agent-submitted listings: `/agents` dashboard, `/admin` review queue (`ADMIN_EMAILS` only, 404 for others), `/listing/<id>` pages, enquiries emailed via Resend (From "<buyer> via AkiyaFind", buyer's message first, one link). Approved listings are mirrored into `listings` with `agent_listing_id`.
- `feeds.py` — agents' Kyero XML feeds. Nothing is fetched until the admin approves a feed (approval = the agent's permission). Each property becomes an approved agent listing (`feed_id`, `feed_ref`, `photo_urls`); properties that leave the feed get `feed_removed_at` and are hidden, unless the feed shrank by more than half (treated as broken). Agent/admin decisions on single listings (withdrawn, rejected) survive re-imports. Feed and photo URLs are agent-controlled: they're only fetched if they resolve to public addresses. Currency is never guessed: a non-yen price becomes "Price on request". Run daily by `.github/workflows/feeds.yml`.
- `alerts.py` — saved-search email alerts (Resend); runs after the daily feed import. `PUBLISHED_SQL` defines which listings may be shown anywhere: agent listings only.
- `pages.py` — `render_page()`: shared footer, Umami tag (`data-domains` limits it to akiyafind.com), `{{CONTACT_EMAIL}}`.
- `schema.py` — listing columns (`created_at`, `last_seen_at`, `delisted_at`, `agent_listing_id`); only ALTERs when a column is missing.
- `.github/workflows/feeds.yml` — daily feed import + alerts. `.github/workflows/purge-athome.yml` — one-off, manual: deletes the old At Home copies (type DELETE to confirm).
- Front-end pages build listing cards as HTML strings: every listing field goes through `esc()` because titles, towns and agent names come from agents and their feeds.

## Environment variables (Railway; names only)
`DATABASE_URL`, `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BASE_URL`, `CONTACT_EMAIL`, `ADMIN_EMAILS`,
`RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `STRIPE_SECRET_KEY`, `STRIPE_SEARCHER_PRICE`, `STRIPE_WEBHOOK_SECRET`, `UMAMI_WEBSITE_ID` (defaults on; `off` disables).
GitHub Actions secrets: `DATABASE_URL`, `RESEND_API_KEY`, `ALERT_FROM_EMAIL`.
Railway stages variable changes: they apply only after clicking **Deploy**, not on a git push.

## Rules this project has settled on
- Only use listing sources with permission. Respect robots.txt and 403s; never disguise a crawler. Private agencies' terms usually forbid scraping, so ask for a feed instead.
- At Home (akiya-athome.jp, athome.jp) declined permission on 29 Oct 2026 and asked for its listings and photos to be removed. Its crawler was deleted. Never crawl, proxy or display its data, or copies of it from other aggregators. Municipal akiya bank data belongs to each municipality: ask the municipality.
- Never show a stock photo for a listing; use the "No photo available" placeholder. Unknown prices are `null` ("Price on request"), never 0, which renders as FREE.
- Every claim on the site must be true: counts come from `/api/counts`; show no number rather than a misleading one.
- Work on a `claude/...` branch, open a PR, and merge only when the owner says "merge PR N". After a deploy, verify on the live site.

## Tests
`tests/` holds end-to-end suites that run against a **local** Postgres. They DROP tables, so `tests/_guard.py` refuses any non-local `DATABASE_URL`.
```
pg_ctlcluster 16 main start   # or any local Postgres with user/db akiya:akiya
python tests/test_agents.py    # also: test_feeds, test_alerts, test_proxy
```
Each test prints PASS/FAIL lines and a final `FAILURES: none` / `ALL PASSED`. Browser checks were run ad hoc with Playwright (Node) and aren't stored here.
