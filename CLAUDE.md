# AkiyaFind — notes for Claude

English-language search site for akiya and affordable homes in Japan, for buyers overseas.
FastAPI + Postgres on **Railway**; every merge to `master` auto-deploys in ~30–90 s.
Domain `akiyafind.com` (DNS at Namecheap: ALIAS `@` → Railway, MX → ImprovMX forwarding).
`www.akiyafind.com` redirects to `akiyafind.com` (middleware in `server.py`).
This repo is **public**: keep business plans, outreach lists and personal details out of it.

## Code map
- `server.py` — routes, `/api/search` + `/api/listings` (with `total` and `freshness`), `/api/img` image proxy (allowlisted hosts, redirects re-checked), auth (Google), Stripe checkout (hidden until all Stripe vars are set), www redirect.
- `agents.py` — agent-submitted listings: `/agents` dashboard, `/admin` review queue (`ADMIN_EMAILS` only, 404 for others), `/listing/<id>` pages, enquiries emailed via Resend. Approved listings are mirrored into `listings` with `agent_listing_id`.
- `alerts.py` — saved-search email alerts (Resend); runs after each crawl in the GitHub workflow.
- `pages.py` — `render_page()`: shared footer, Umami tag (`data-domains` limits it to akiyafind.com), `{{CONTACT_EMAIL}}`.
- `schema.py` — listing columns (`created_at`, `last_seen_at`, `delisted_at`, `agent_listing_id`) and `crawl_log`; only ALTERs when a column is missing.
- `crawler_v3.py` — akiya-athome.jp crawler. Identifies as `AkiyaFindBot`, honours robots.txt, stops the whole run on 401/403/429. Only *complete* prefecture crawls delist, and never agent listings.
- `.github/workflows/crawl.yml` — daily crawl + alerts. The root `crawl.yml` and `.github/crawl.yml` are stale copies; GitHub ignores them.

## Environment variables (Railway; names only)
`DATABASE_URL`, `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BASE_URL`, `CONTACT_EMAIL`, `ADMIN_EMAILS`,
`RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `STRIPE_SECRET_KEY`, `STRIPE_SEARCHER_PRICE`, `STRIPE_WEBHOOK_SECRET`, `UMAMI_WEBSITE_ID` (defaults on; `off` disables).
GitHub Actions secrets: `DATABASE_URL`, `RESEND_API_KEY`, `ALERT_FROM_EMAIL`.
Railway stages variable changes: they apply only after clicking **Deploy**, not on a git push.

## Rules this project has settled on
- Only use listing sources with permission. Respect robots.txt and 403s; never disguise the crawler. Private agencies' terms usually forbid scraping, so ask for a feed instead.
- Never show a stock photo for a listing; use the "No photo available" placeholder. Unknown prices are `null` ("Price on request"), never 0, which renders as FREE.
- Every claim on the site must be true: counts come from `/api/counts`, and there's no "live data" unless crawls are current.
- Work on a `claude/...` branch, open a PR, and merge only when the owner says "merge PR N". After a deploy, verify on the live site.

## Tests
`tests/` holds end-to-end suites that run against a **local** Postgres. They DROP tables, so `tests/_guard.py` refuses any non-local `DATABASE_URL`.
```
pg_ctlcluster 16 main start   # or any local Postgres with user/db akiya:akiya
python tests/test_agents.py    # also: test_alerts, test_delist, test_blocked, test_parse, test_proxy
```
Each test prints PASS/FAIL lines and a final `FAILURES: none` / `ALL PASSED`. Browser checks were run ad hoc with Playwright (Node) and aren't stored here.
