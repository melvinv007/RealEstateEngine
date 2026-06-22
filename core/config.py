"""
config.py
All configuration constants — change here, affects entire system.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── API Keys ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_KEY = os.getenv("API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_MAPS_API_KEY"   # for geocoding

# ── API Usage Toggles ─────────────────────────────────────────────────────────
# ingestion/parser.py — Gemini classifier in is_real_estate_message()
USE_GEMINI_PARSER_CLASSIFIER = False
# ingestion/parser.py — Gemini extraction in parse_text_message()
USE_GEMINI_PARSER_TEXT_EXTRACTION = True
# ingestion/parser.py — Gemini extraction in parse_image()
USE_GEMINI_PARSER_IMAGE_EXTRACTION = True
# location/resolver.py — Gemini disambiguation in _gemini_disambiguate()
USE_GEMINI_RESOLVER_DISAMBIGUATE = True
# location/resolver.py — Gemini confirmation in _gemini_confirm()
USE_GEMINI_RESOLVER_CONFIRM = True
# location/resolver.py — Gemini cold Step A in _gemini_cold()
USE_GEMINI_RESOLVER_COLD_STEP_A = True
# location/resolver.py — Gemini cold Step B in _gemini_cold()
USE_GEMINI_RESOLVER_COLD_STEP_B = True
# location/geocoder.py — Nominatim geocoding in _geocode_raw()
USE_NOMINATIM_GEOCODING = True

# ── WhatsApp Ingest Settings ─────────────────────────────────────────────────
WA_FIELD_RAW_MESSAGE = "raw_message"
WA_FIELD_MESSAGE_ID = "message_id"
WA_FIELD_PHONE_NUMBER = "phone_number"
WA_TIMESTAMP_FIELD = "received_at"

WA_STORED_MESSAGE_ID = "wa_message_id"
WA_STORED_PHONE_NUMBER = "wa_phone_number"
WA_STORED_RECEIVED_AT = "wa_received_at"

WA_NO_MATCH_BUY_MESSAGE = (
    "No matching listings found at this time. "
    "Your requirement has been saved and you will be notified when a match is found."
)
WA_NO_MATCH_SELL_MESSAGE = (
    "Your listing has been stored. "
    "We will notify you when a matching buyer is found."
)

WA_BUY_MATCH_HEADER_BROKER_ONLY = "We found {n} broker listing(s) for your requirement:"
WA_BUY_MATCH_HEADER_PROJECT_ONLY = "We found {n} project(s) for your requirement:"
WA_BUY_MATCH_HEADER_BOTH = "We found {nb} broker listing(s) and {np} project(s) for your requirement:"
WA_SELL_MATCH_HEADER = "Your listing matched {n} potential buyer(s):"
WA_BROKER_SELL_TEMPLATE = (
    "[Broker Sell] {property_type} {bhk}BR at {location} — "
    "AED {price:,.0f} | Contact: {broker_name} {broker_phone}"
)
WA_BROKER_BUY_TEMPLATE = (
    "[Buyer] Looking for {property_type} {bhk}BR in {location} | "
    "Budget: AED {price:,.0f} | Contact: {broker_name} {broker_phone}"
)
WA_PROJECT_TEMPLATE = (
    "[New Project] {project_name} by {developer} in {area} | "
    "Starting AED {starting_price:,.0f} | {bedrooms} | "
    "Handover: {handover} | {payment_plan} | PDF: {pdf_link}"
)

# ── MongoDB ───────────────────────────────────────────────────────────────────
# MONGO_URI = "mongodb://localhost:27017"             # or Atlas URI
# MONGO_URI = "mongodb+srv://fonot94264_db_user:3xp9bBPec8jkTFQS@cluster0.rkuyemc.mongodb.net/?appName=Cluster0"             # or Atlas URI
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = "realestate"
COLLECTION_BUY = "buy_listings"
COLLECTION_SELL = "sell_listings"
COLLECTION_MATCHES = "matches"
COLLECTION_PROJECTS = "projects"
COLLECTION_PROJECT_MATCHES = "project_matches"

PROJECTS_MASTER_EXCEL = "data/projects_master.xlsx"
PROJECTS_MASTER_EXCEL = "data/projects_master.csv"
LOCATION_MASTER_EXCEL = "data/location_master.xlsx"
PIPELINE_STATE_FILE = "cache/pipeline_state.json"
PIPELINE_WATCHER_LOG = "cache/pipeline_watcher.log"
GEOCODE_FAILURES_FILE = "cache/geocode_failures.json"

# ── Matching Tolerances ───────────────────────────────────────────────────────
PRICE_TOLERANCE = 0.15          # ±15% — change this one line to adjust
DISTANCE_KM_TOLERANCE = 3.0     # ±3 km — change this one line to adjust
BHK_TOLERANCE = 1               # 0 = exact match; 1 = allow ±1 bedroom
SQFT_TOLERANCE = 0.20           # ±20% on sqft (set to None to skip sqft matching)

# MODEL = "gemini-3.1-flash-lite"

GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3-flash-preview"
]
GROQ_MODELS = [
    "llama-3.3-70b-versatile"
]
OPENROUTER_MODELS = [
    "deepseek/deepseek-v4-flash:free",
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-120b:free"
]

# ── Geocoding Cache ───────────────────────────────────────────────────────────
# Avoids re-geocoding the same location repeatedly
GEOCODE_CACHE_FILE = str(_PROJECT_ROOT / "cache" / "geocode_cache.json")
COORDINATES_CSV = str(_PROJECT_ROOT / "data" / "coordinates.csv")  # canonical_name,lat,lng
USE_LEGACY_GEOCODING = False  # True = use cache + fuzzy + Nominatim fallback; False = coordinates.csv only

# ── Location Resolution ──────────────────────────────────────────────────────
LOCATIONS_CSV = str(_PROJECT_ROOT / "data" / "locations.csv")
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
LOCATION_RESOLUTION_CACHE_FILE = str(_PROJECT_ROOT / "cache" / "location_cache.json")
# Log file for locations that failed all resolution layers
UNRESOLVED_LOG_FILE = str(_PROJECT_ROOT / "cache" / "unresolved_locations.log")

# Location tree
LOCATION_TREE_CACHE = str(_PROJECT_ROOT / "cache" / "location_tree.json")
UNIQUE_NAMES_CACHE = str(_PROJECT_ROOT / "cache" / "unique_names.json")
FORCE_REBUILD_TREE = False

# Resolver tuning (new)
LOCATION_MIN_CONFIDENCE = 0.65
LOCATION_HIGH_CONFIDENCE = 0.82
LOCATION_AMBIGUITY_BAND = 0.08
USE_EMBEDDINGS = False

# Alias generator
ALIAS_GEN_DELAY_SECONDS = 2
ALIAS_GEN_TIER3_SKIP = True

# ── Match Quality Gate ────────────────────────────────────────────────────────
# Minimum score (0.0–1.0) for a match to be recorded
MIN_MATCH_SCORE = 0.75
 
# Reject match if both price AND location are skipped (no hard evidence)
REQUIRE_PRICE_OR_LOCATION = True
 
# ── Fuzzy Location Matching ───────────────────────────────────────────────────
# Catches typos like "Businessbay", "Dubai marinaa" before geocoding
FUZZY_LOCATION_THRESHOLD = 85       # 0–100 similarity score
 
# ── Duplicate Detection ───────────────────────────────────────────────────────
DUPLICATE_DETECTION_BUY = True
DUPLICATE_DETECTION_SELL = True

# ── Raw-Message Fuzzy Dedupe ──────────────────────────────────────────────────
# "off"    = disabled. Legacy field-level dedupe in database.py runs exactly as today.
# "shadow" = runs the fuzzy raw-text check and logs what it WOULD do, but always
#            calls the LLM and stores normally — use this to validate before trusting it.
# "active" = on a high-confidence match, skips the LLM call and clones the prior parse.
RAW_DEDUPE_MODE = "off"

RAW_DEDUPE_TEXT_THRESHOLD = 93          # rapidfuzz token_set_ratio cutoff (0-100)
RAW_DEDUPE_REQUIRE_NUMERIC_MATCH = True # guards against "same template, different price/BHK"
RAW_DEDUPE_SINGLE_LISTING_ONLY = True   # never fast-path messages that might have >1 listing
RAW_DEDUPE_CANDIDATE_LOOKBACK = 500     # how many recent raw messages to fuzzy-scan

# ±5% price tolerance for duplicate detection.
# Example: 3.00M and 3.12M are duplicate-price-compatible, 3.00M and 3.40M are not.
DUPLICATE_PRICE_TOLERANCE = 0.10

# ±10% size tolerance for duplicate detection.
# Used for sqft and plot_sqft duplicate checks.
DUPLICATE_SIZE_TOLERANCE = 0.10

# Raw text fuzzy similarity threshold.
# 92 is strict enough to catch reposts while avoiding unrelated listings.
DUPLICATE_RAW_TEXT_FUZZY_THRESHOLD = 92

# Minimum number of non-null matched fields required.
# Important rule: at least one important field must also match.
DUPLICATE_MIN_FIELD_MATCHES = 3

# Max candidate docs checked per duplicate query.
# Keeps duplicate detection fast even when DB grows.
DUPLICATE_CANDIDATE_LIMIT = 80

 
# ── Behavior ──────────────────────────────────────────────────────────────────
DELETE_AFTER_MATCH = False      # True = delete matched listings after match


LOCATION_CONFIDENCE_HIGH = 0.82
LOCATION_CONFIDENCE_AMBIGUITY_BAND = 0.08
LOCATION_CONFIDENCE_CONFIRM = 0.65

# When True, reduce console verbosity and suppress diagnostic logs
# Set this in the environment on production hosts (e.g. EC2)
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "False").lower() in ("1", "true", "yes")
