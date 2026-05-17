from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import psycopg2
import os

app = FastAPI()

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_listings():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT title_en, prefecture, city, price_jpy, size_m2, source_name
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
            'is_free': row[3] == 0
        })
    return listings

@app.get("/api/listings")
def api_listings():
    listings = get_listings()
    return JSONResponse(content=listings)

@app.get("/", response_class=HTMLResponse)
def homepage():
    with open("index.html") as f:
        return f.read()

