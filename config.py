"""
config.py
All configuration constants — change here, affects entire system.
"""

# ── API Keys ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyB1kWvpDa2RbbkIzcMjDib2mhm2PWDNiVg"
# GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_MAPS_API_KEY"   # for geocoding

# ── MongoDB ───────────────────────────────────────────────────────────────────
# MONGO_URI = "mongodb://localhost:27017"             # or Atlas URI
# MONGO_URI = "mongodb+srv://fonot94264_db_user:3xp9bBPec8jkTFQS@cluster0.rkuyemc.mongodb.net/?appName=Cluster0"             # or Atlas URI
MONGO_URI = "mongodb://fonot94264_db_user:3xp9bBPec8jkTFQS@ac-b3w3hiq-shard-00-00.rkuyemc.mongodb.net:27017,ac-b3w3hiq-shard-00-01.rkuyemc.mongodb.net:27017,ac-b3w3hiq-shard-00-02.rkuyemc.mongodb.net:27017/?ssl=true&replicaSet=atlas-13s27k-shard-0&authSource=admin&appName=Cluster0"             # or Atlas URI
MONGO_DB_NAME = "realestate"
COLLECTION_BUY = "buy_listings"
COLLECTION_SELL = "sell_listings"
COLLECTION_MATCHES = "matches"

# ── Matching Tolerances ───────────────────────────────────────────────────────
PRICE_TOLERANCE = 0.1          # ±10% — change this one line to adjust
DISTANCE_KM_TOLERANCE = 2.0     # ±2 km — change this one line to adjust
BHK_TOLERANCE = 0               # 0 = exact match; 1 = allow ±1 bedroom
SQFT_TOLERANCE = 0.15           # ±15% on sqft (set to None to skip sqft matching)

MODEL = "gemini-2.5-flash"

# ── Geocoding Cache ───────────────────────────────────────────────────────────
# Avoids re-geocoding the same location repeatedly
GEOCODE_CACHE_FILE = "geocode_cache.json"

# ── Match Quality Gate ────────────────────────────────────────────────────────
# Minimum score (0.0–1.0) for a match to be recorded
MIN_MATCH_SCORE = 0.75
 
# Reject match if both price AND location are skipped (no hard evidence)
REQUIRE_PRICE_OR_LOCATION = True
 
# ── Fuzzy Location Matching ───────────────────────────────────────────────────
# Catches typos like "Businessbay", "Dubai marinaa" before geocoding
FUZZY_LOCATION_THRESHOLD = 85       # 0–100 similarity score
 
# ── Duplicate Detection ───────────────────────────────────────────────────────
DUPLICATE_DETECTION = True
DUPLICATE_PRICE_TOLERANCE = 0.02    # ±2% for fingerprint price comparison (tighter than match)
 
# ── Geocoding / Location Normalization ────────────────────────────────────────
GEOCODE_CACHE_FILE = "geocode_cache.json"
# Uses Gemini to normalize broker slang → official Dubai area names
# e.g. "Greenway 2" → "Emaar South", "Costa Brava" → "DAMAC Lagoons"
USE_GEMINI_LOCATION_NORMALIZER = True
 
# ── Behavior ──────────────────────────────────────────────────────────────────
DELETE_AFTER_MATCH = False      # True = delete matched listings after match
 
