from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import psycopg2
import os

app = FastAPI()

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_listings():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT title_en, prefecture, city, price_jpy, size_m2, source_name, source_url
        FROM listings
        ORDER BY created_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    listings = []
    for row in rows:
        listings.append({
            'title': row[0],
            'prefecture': row[1],
            'city': row[2],
            'price_jpy': row[3],
            'price_aud': round(row[3] * 0.0096, 0) if row[3] else 0,
            'size_m2': float(row[4]) if row[4] else 0,
            'source': row[5],
            'source_url': row[6] if row[6] else '',
            'is_free': row[3] == 0
        })
    return listings

@app.get("/api/listings")
def api_listings():
    listings = get_listings()
    return JSONResponse(content=listings)

@app.get("/api/search")
def api_search(q: str = "", prefecture: str = "", min_price: int = 0, max_price: int = 0):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    query = """
        SELECT title_en, prefecture, city, price_jpy, size_m2, source_name, source_url
        FROM listings
        WHERE 1=1
    """
    params = []

    if q:
        query += " AND (LOWER(city) LIKE %s OR LOWER(prefecture) LIKE %s OR LOWER(title_en) LIKE %s)"
        like = f"%{q.lower()}%"
        params += [like, like, like]

    if prefecture:
        query += " AND LOWER(prefecture) = %s"
        params.append(prefecture.lower())

    if min_price > 0:
        query += " AND price_jpy >= %s"
        params.append(min_price)

    if max_price > 0:
        query += " AND price_jpy <= %s"
        params.append(max_price)

    query += " ORDER BY created_at DESC LIMIT 100"

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    listings = []
    for row in rows:
        listings.append({
            'title': row[0],
            'prefecture': row[1],
            'city': row[2],
            'price_jpy': row[3],
            'price_aud': round(row[3] * 0.0096, 0) if row[3] else 0,
            'size_m2': float(row[4]) if row[4] else 0,
            'source': row[5],
            'source_url': row[6] if row[6] else '',
            'is_free': row[3] == 0
        })
    return JSONResponse(content=listings)

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
