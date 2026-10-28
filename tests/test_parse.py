import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: F401  (refuses non-local databases)
import importlib.util
os.environ["DATABASE_URL"] = "unused"

import crawler_v3 as new
old = new  # old_parse() below reimplements the old card walk; it only borrows parse_price

def card(i, img_attrs, depth=1, extra=""):
    inner = (f'<div class="imageCenter"><img {img_attrs}></div>'
             f'<a href="/bukken/detail/L{i}/?ref=list">物件{i}</a><p>長野県松本市</p><p>{i*100}万円</p><p>{i*10+50}㎡</p>{extra}')
    for _ in range(depth):
        inner = f"<div>{inner}</div>"
    return f'<li class="item">{inner}</li>'

def page(cards, wrap=3):
    body = "<ul>" + "".join(cards) + "</ul>"
    for _ in range(wrap):
        body = f"<div>{body}</div>"
    return f"<html><body>{body}</body></html>"

def show(name, html):
    o, n = old.parse_listings(html, "Nagano") if hasattr(old, "parse_listings") else None, new.parse_listings(html, "Nagano")
    return n

failures = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: failures.append(msg)

# Old parser, reconstructed from the committed version's inline loop, run through a minimal shim
def old_parse(html):
    import re
    from bs4 import BeautifulSoup
    out = []
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=re.compile(r'/bukken/detail/')):
        c = a
        for _ in range(5):
            p = c.parent
            if not p or p.name in ('body', 'html', '[document]'): break
            c = p
        lines = [l for l in c.get_text(separator='\n', strip=True).split('\n') if l.strip()]
        img = ''
        d = c.find('div', class_='imageCenter')
        if d and d.find('img') and d.find('img').get('src'):
            img = d.find('img')['src']; img = 'https:' + img if img.startswith('//') else img
        price = next((old.parse_price(l) for l in lines if '万円' in l or '円' in l), None)
        out.append({"title": lines[0] if lines else '', "img": img, "price": price})
    return out

# 1. Shallow cards: old walk-up overshoots into the list
html = page([card(i, f'src="//img.athome.jp/p{i}.jpg"') for i in range(1, 4)])
o = old_parse(html); n = new.parse_listings(html, "Nagano")
print("  old:", [(x["title"], x["img"][-6:], x["price"]) for x in o])
print("  new:", [(x["title_jp"], x["image_url"][-6:], x["price_jpy"]) for x in n])
check(len({x["img"] for x in o}) == 1, "old parser gives every shallow card the SAME photo (bug reproduced)")
check([x["image_url"] for x in n] == [f"https://img.athome.jp/p{i}.jpg" for i in range(1, 4)], "new: each card gets its own photo")
check([x["price_jpy"] for x in n] == [1000000, 2000000, 3000000], "new: each card gets its own price")
check([x["title_jp"] for x in n] == ["物件1", "物件2", "物件3"], "new: each card gets its own title")

# 2. Deep cards: image is 7 levels above the link
deep = lambda i: (f'<li><div class="imageCenter"><img src="//img.athome.jp/d{i}.jpg"></div>'
                  + "<div>"*7 + f'<a href="/bukken/detail/D{i}/">D{i}</a>' + "</div>"*7 + f'<p>{i}00万円</p></li>')
html = page([deep(i) for i in range(1, 3)])
check([x["image_url"] for x in new.parse_listings(html, "Nagano")] == ["https://img.athome.jp/d1.jpg", "https://img.athome.jp/d2.jpg"], "new: deep card finds its photo")

# 3. Lazy loading: src is a spacer, real photo in data-src / srcset
html = page([card(1, 'src="/img/spacer.gif" data-src="//img.athome.jp/real1.jpg"'),
             card(2, 'src="data:image/gif;base64,R0lGOD" srcset="//img.athome.jp/s2.jpg 320w, //img.athome.jp/l2.jpg 800w"')])
check([x["image_url"] for x in new.parse_listings(html, "Nagano")] == ["https://img.athome.jp/real1.jpg", "https://img.athome.jp/l2.jpg"], "new: lazy-load photos use real URL (largest srcset)")

# 4. No-image placeholders are dropped, relative URLs made absolute
html = page([card(1, 'src="/common/img/noimage.gif"'), card(2, 'src="/photo/2.jpg"'), card(3, 'src="//img.athome.jp/no_photo.png"')])
check([x["image_url"] for x in new.parse_listings(html, "Nagano")] == ["", "https://www.akiya-athome.jp/photo/2.jpg", ""], "new: placeholders dropped, relative src resolved")

# 5. Photo only reachable as an image inside the listing link (no imageCenter div)
html = page([f'<li><a href="/bukken/detail/P{i}/"><img src="//img.athome.jp/a{i}.jpg"></a><a href="/bukken/detail/P{i}/?tab=map">map</a><p>{i}00万円</p></li>' for i in (1, 2)])
n = new.parse_listings(html, "Nagano")
check([x["image_url"] for x in n] == ["https://img.athome.jp/a1.jpg", "https://img.athome.jp/a2.jpg"], "new: photo inside listing link; ?tab= variant treated as same listing")
check([x["source_url"] for x in n] == ["https://www.akiya-athome.jp/bukken/detail/P1/", "https://www.akiya-athome.jp/bukken/detail/P2/"], "one row per listing; first link's URL stored as before")
html = page([card(1, 'src="//img.athome.jp/q.jpg"')])
check(new.parse_listings(html, "Nagano")[0]["source_url"] == "https://www.akiya-athome.jp/bukken/detail/L1/?ref=list", "stored source_url keeps its query string (matches existing DB rows)")

# 6. Table layout: photos in one row, details in another -> no wrong photo, just none
html = ("<html><body><table><tr>" + "".join(f'<td><img src="//img.athome.jp/t{i}.jpg"></td>' for i in (1, 2)) + "</tr><tr>"
        + "".join(f'<td><a href="/bukken/detail/T{i}/">T{i}</a></td>' for i in (1, 2)) + "</tr></table></body></html>")
check([x["image_url"] for x in new.parse_listings(html, "Nagano")] == ["", ""], "new: ambiguous layout gives no photo rather than a wrong one")

print("FAILURES:", failures or "none")
sys.exit(1 if failures else 0)
