import psycopg2
import os
import time
import urllib.request
import json

DATABASE_URL = os.environ.get('DATABASE_URL')

def geocode(city, prefecture):
    """Look up lat/lng for a Japanese city using OpenStreetMap Nominatim"""
    # Try city first, then fall back to prefecture
    queries = [
        city + ', Japan',
        prefecture + ', Japan',
    ]
    
    for query in queries:
        try:
            url = 'https://nominatim.openstreetmap.org/search?q=' + urllib.parse.quote(query) + '&format=json&limit=1&countrycodes=jp'
            req = urllib.request.Request(url, headers={'User-Agent': 'AkiyaFind/1.0 geocoder'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                if data:
                    return float(data[0]['lat']), float(data[0]['lon'])
        except:
            pass
        time.sleep(1)
    
    return None, None

import urllib.parse

def run():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Get all listings without coordinates
    cur.execute("SELECT id, city, prefecture FROM listings WHERE lat IS NULL")
    rows = cur.fetchall()
    print("Listings to geocode: " + str(len(rows)))
    
    # Cache so we don't hit the API for duplicate cities
    cache = {}
    done = 0
    failed = 0
    
    for row in rows:
        id_, city, prefecture = row
        cache_key = city + '|' + prefecture
        
        if cache_key in cache:
            lat, lng = cache[cache_key]
        else:
            lat, lng = geocode(city, prefecture)
            cache[cache_key] = (lat, lng)
            time.sleep(1)  # Nominatim rate limit: 1 req/sec
        
        if lat and lng:
            cur.execute("UPDATE listings SET lat=%s, lng=%s WHERE id=%s", (lat, lng, id_))
            conn.commit()
            done += 1
            if done % 50 == 0:
                print("  Progress: " + str(done) + " done, " + str(failed) + " failed")
        else:
            failed += 1
    
    print("\nDone! " + str(done) + " geocoded, " + str(failed) + " failed")
    cur.close()
    conn.close()

run()
