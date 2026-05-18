"""
config.py
All configuration constants — change here, affects entire system.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_KEY = os.getenv("API_KEY")
# GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_MAPS_API_KEY"   # for geocoding

# ── API Usage Toggles ─────────────────────────────────────────────────────────
# parser.py — Gemini classifier in is_real_estate_message()
USE_GEMINI_PARSER_CLASSIFIER = False
# parser.py — Gemini extraction in parse_text_message()
USE_GEMINI_PARSER_TEXT_EXTRACTION = True
# parser.py — Gemini extraction in parse_image()
USE_GEMINI_PARSER_IMAGE_EXTRACTION = True
# location_resolver.py — Gemini disambiguation in _gemini_disambiguate()
USE_GEMINI_RESOLVER_DISAMBIGUATE = True
# location_resolver.py — Gemini confirmation in _gemini_confirm()
USE_GEMINI_RESOLVER_CONFIRM = True
# location_resolver.py — Gemini cold Step A in _gemini_cold()
USE_GEMINI_RESOLVER_COLD_STEP_A = True
# location_resolver.py — Gemini cold Step B in _gemini_cold()
USE_GEMINI_RESOLVER_COLD_STEP_B = True
# geocoder.py — Nominatim geocoding in _geocode_raw()
USE_NOMINATIM_GEOCODING = True

# ── MongoDB ───────────────────────────────────────────────────────────────────
# MONGO_URI = "mongodb://localhost:27017"             # or Atlas URI
# MONGO_URI = "mongodb+srv://fonot94264_db_user:3xp9bBPec8jkTFQS@cluster0.rkuyemc.mongodb.net/?appName=Cluster0"             # or Atlas URI
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = "realestate"
COLLECTION_BUY = "buy_listings"
COLLECTION_SELL = "sell_listings"
COLLECTION_MATCHES = "matches"

# ── Matching Tolerances ───────────────────────────────────────────────────────
PRICE_TOLERANCE = 0.1          # ±10% — change this one line to adjust
DISTANCE_KM_TOLERANCE = 2.0     # ±2 km — change this one line to adjust
BHK_TOLERANCE = 0               # 0 = exact match; 1 = allow ±1 bedroom
SQFT_TOLERANCE = 0.15           # ±15% on sqft (set to None to skip sqft matching)

MODEL = "gemini-2.5-flash-lite"

# ── Geocoding Cache ───────────────────────────────────────────────────────────
# Avoids re-geocoding the same location repeatedly
GEOCODE_CACHE_FILE = "geocode_cache.json"
COORDINATES_CSV = "coordinates.csv" # canonical_name,lat,lng
USE_LEGACY_GEOCODING = False  # True = use cache + fuzzy + Nominatim fallback; False = coordinates.csv only

# ── Location Resolution ──────────────────────────────────────────────────────
LOCATIONS_CSV = "locations.csv"
# Column names in the CSV/Excel — change here if the sheet headers change
LOCATION_CSV_CANONICAL_COLUMN = "canonical_name"
LOCATION_CSV_ALIASES_COLUMN = "aliases"
# Same for Excel conversion script
LOCATION_EXCEL_CANONICAL_COLUMN = "canonical_name"
LOCATION_EXCEL_ALIASES_COLUMN = "aliases"
# Fuzzy matching threshold for Layer 1 (0-100). Default 80.
# Lower = more permissive (more matches, more false positives)
# Higher = stricter (fewer matches, more falls through to Gemini)
LOCATION_FUZZY_THRESHOLD = 80
# Cache file for resolved locations (separate from geocode cache)
LOCATION_RESOLUTION_CACHE_FILE = "location_cache.json"
# Log file for locations that failed all resolution layers
UNRESOLVED_LOG_FILE = "unresolved_locations.log"

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
DUPLICATE_PRICE_TOLERANCE = 0.05    # ±5% for fingerprint price comparison (tighter than match)

 
# ── Behavior ──────────────────────────────────────────────────────────────────
DELETE_AFTER_MATCH = False      # True = delete matched listings after match


LOCATION_CONFIDENCE_HIGH = 0.82
LOCATION_CONFIDENCE_AMBIGUITY_BAND = 0.08
LOCATION_CONFIDENCE_CONFIRM = 0.65
