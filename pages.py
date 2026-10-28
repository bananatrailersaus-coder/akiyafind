"""
pages.py — AkiyaFind page rendering shared by every HTML route.

Each page is a static file; render_page() adds what every page needs:
the opt-in analytics tag, the site footer, and the contact email.
"""

import html
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Privacy-friendly analytics (Umami: no cookies, no personal data). The website
# ID is public (it's in every page), so it defaults to AkiyaFind's; set
# UMAMI_WEBSITE_ID=off to disable. data-domains stops local/test copies of the
# site from being counted.
UMAMI_WEBSITE_ID = (os.environ.get("UMAMI_WEBSITE_ID") or "02f86e1e-9e5a-4ebb-ad59-e9c03f227009").strip()
if UMAMI_WEBSITE_ID.lower() == "off":
    UMAMI_WEBSITE_ID = ""
UMAMI_SCRIPT_URL = os.environ.get("UMAMI_SCRIPT_URL", "").strip() or "https://cloud.umami.is/script.js"
UMAMI_DOMAINS = os.environ.get("UMAMI_DOMAINS", "").strip() or "akiyafind.com,www.akiyafind.com"

# Shown on the privacy and terms pages and in the footer.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()

FOOTER = """
<style>
  .af-footer { background: #1a1208; color: #d9d2c5; padding: 28px 16px; font-family: 'DM Sans', Arial, sans-serif; font-size: 0.85rem; }
  .af-footer-inner { max-width: 1200px; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 12px 28px; align-items: center; justify-content: space-between; }
  .af-footer a { color: #d9d2c5; text-decoration: none; }
  .af-footer a:hover { color: #fff; text-decoration: underline; }
  /* a div, not <nav>: every page styles nav (white, sticky), which would leak in here */
  .af-footer .af-links { display: flex; flex-wrap: wrap; gap: 8px 20px; }
</style>
<footer class="af-footer">
  <div class="af-footer-inner">
    <span>© AkiyaFind · Listings link to their original source</span>
    <div class="af-links">
      <a href="/search">Browse</a>
      <a href="/agents">For agents: list your properties</a>
      <a href="/privacy">Privacy</a>
      <a href="/terms">Terms</a>
      __CONTACT__
    </div>
  </div>
</footer>
"""


def contact_html():
    if not CONTACT_EMAIL:
        return "[contact email not set]"
    e = html.escape(CONTACT_EMAIL)
    return f'<a href="mailto:{e}">{e}</a>'


def render_page(name, footer=True):
    with open(os.path.join(BASE_DIR, name)) as f:
        page = f.read()
    page = page.replace("{{CONTACT_EMAIL}}", contact_html())
    if UMAMI_WEBSITE_ID:
        tag = (f'<script defer src="{html.escape(UMAMI_SCRIPT_URL)}" '
               f'data-website-id="{html.escape(UMAMI_WEBSITE_ID)}" '
               f'data-domains="{html.escape(UMAMI_DOMAINS)}"></script>')
        page = page.replace("</head>", tag + "\n</head>", 1)
    if not footer:
        return page
    # replace(), not format(): the footer's CSS is full of braces
    footer = FOOTER.replace("__CONTACT__", contact_html() if CONTACT_EMAIL else "")
    # rpartition: insert before the last </body>, so page scripts mentioning it are untouched
    head, sep, tail = page.rpartition("</body>")
    return head + footer + sep + tail if sep else page + footer
