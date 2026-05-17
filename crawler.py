import requests
from bs4 import BeautifulSoup
import psycopg2
import os
import time

# Database connection
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def save_listing(listing):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO listings 
            (source_url, prefecture, city, title_jp, title_en, 
             price_jpy, size_m2, source_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_url) DO NOTHING
        """, (
            listing['source_url'],
            listing['prefecture'],
            listing['city'],
            listing['title_jp'],
            listing['title_en'],
            listing['price_jpy'],
            listing['size_m2'],
            listing['source_name']
        ))
        conn.commit()
        print(f"Saved: {listing['title_en']}")
    except Exception as e:
        print(f"Error saving: {e}")
    finally:
        cur.close()
        conn.close()

def crawl_nagano():
    print("Crawling Nagano akiya bank...")
    url = "https://www.pref.nagano.lg.jp/rinsei/sangyo/rinsei/akiyabank/index.html"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; AkiyaFind/1.0)'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all listing links
        listings = []
        links = soup.find_all('a', href=True)
        
        for link in links:
            text = link.get_text(strip=True)
            if any(word in text for word in ['空き家', '物件', '住宅']):
                listing = {
                    'source_url': url + link['href'],
                    'prefecture': '長野県',
                    'city': '長野市',
                    'title_jp': text,
                    'title_en': 'Vacant property - Nagano',
                    'price_jpy': 0,
                    'size_m2': 0,
                    'source_name': 'Nagano Prefecture Akiya Bank'
                }
                listings.append(listing)
                print(f"Found: {text}")
        
        return listings
        
    except Exception as e:
        print(f"Crawl error: {e}")
        return []

# Test listings - real data we insert manually to verify the pipeline works
def insert_test_listings():
    test_listings = [
        {
            'source_url': 'https://akiyafind.com/test/listing-001',
            'prefecture': '長野県',
            'city': '上田市',
            'title_jp': '築50年の古民家、庭付き',
            'title_en': '50-year-old kominka farmhouse with garden',
            'price_jpy': 1500000,
            'size_m2': 120,
            'source_name': 'AkiyaFind Test Data'
        },
        {
            'source_url': 'https://akiyafind.com/test/listing-002',
            'prefecture': '北海道',
            'city': '富良野市',
            'title_jp': '農地付き空き家、要リフォーム',
            'title_en': 'Vacant house with farmland - renovation required',
            'price_jpy': 800000,
            'size_m2': 95,
            'source_name': 'AkiyaFind Test Data'
        },
        {
            'source_url': 'https://akiyafind.com/test/listing-003',
            'prefecture': '京都府',
            'city': '京都市',
            'title_jp': '町家、西陣地区、リノベーション済み',
            'title_en': 'Machiya townhouse - Nishijin district - renovated',
            'price_jpy': 12000000,
            'size_m2': 85,
            'source_name': 'AkiyaFind Test Data'
        },
        {
            'source_url': 'https://akiyafind.com/test/listing-004',
            'prefecture': '愛媛県',
            'city': '西予市',
            'title_jp': '無料譲渡物件、海が見える',
            'title_en': 'Free transfer property with ocean views',
            'price_jpy': 0,
            'size_m2': 110,
            'source_name': 'AkiyaFind Test Data'
        },
        {
            'source_url': 'https://akiyafind.com/test/listing-005',
            'prefecture': '宮城県',
            'city': '気仙沼市',
            'title_jp': '山間部の古民家、大きな土地付き',
            'title_en': 'Mountain kominka with large land plot',
            'price_jpy': 3500000,
            'size_m2': 200,
            'source_name': 'AkiyaFind Test Data'
        },
    ]
    
    print("Inserting test listings into database...")
    for listing in test_listings:
        save_listing(listing)
    print("Done! Check your database.")

if __name__ == "__main__":
    insert_test_listings()