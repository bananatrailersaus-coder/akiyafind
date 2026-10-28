import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
import io
os.environ.update(DATABASE_URL="postgresql://akiya:akiya@localhost/akiya", SECRET_KEY="t")

import httpx
import server
from starlette.testclient import TestClient

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
calls = []
def handler(req):
    calls.append(str(req.url))
    u = str(req.url)
    if u == "https://img.athome.jp/ok.jpg": return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    if u == "http://img.athome.jp/redir.jpg": return httpx.Response(301, headers={"location": "https://img.athome.jp/ok.jpg"})
    if u == "https://img.athome.jp/rel.jpg": return httpx.Response(302, headers={"location": "/ok.jpg"})
    if u == "https://img.athome.jp/evil.jpg": return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    if u == "https://img.athome.jp/loop.jpg": return httpx.Response(302, headers={"location": "https://img.athome.jp/loop.jpg"})
    if u == "https://img.athome.jp/html.jpg": return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    return httpx.Response(404)

Real = httpx.AsyncClient
server.httpx.AsyncClient = lambda *a, **k: Real(transport=httpx.MockTransport(handler))
c = TestClient(server.app)
def get(u): return c.get("/api/img", params={"url": u})

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); cond or fails.append(msg)

r = get("https://img.athome.jp/ok.jpg"); check(r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png", "direct image served")
r = get("http://img.athome.jp/redir.jpg"); check(r.status_code == 200 and r.content == PNG, "redirect to allowed host followed")
r = get("https://img.athome.jp/rel.jpg"); check(r.status_code == 200 and r.content == PNG, "relative redirect followed")
calls.clear(); r = get("https://img.athome.jp/evil.jpg")
check(r.status_code == 404 and not any("169.254" in x for x in calls), "redirect to internal address refused, never fetched")
r = get("https://img.athome.jp/loop.jpg"); check(r.status_code == 404, "redirect loop stops")
r = get("https://img.athome.jp/html.jpg"); check(r.status_code == 404, "non-image refused")
r = get("https://evil.com/x.jpg"); check(r.status_code == 400, "disallowed host refused up front")
print("FAILURES:", fails or "none"); sys.exit(1 if fails else 0)
