"""
geocoder.py
Location normalization pipeline:
  1. Fuzzy string match  (rapidfuzz) — catches typos only, NOT phase variants
    2. Nominatim (OpenStreetMap) geocoding — free, no API key needed
    3. Haversine distance

All results are cached locally (geocode_cache.json).
"""

import csv
import json
import math
import os
import re
import time
from rapidfuzz import fuzz
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from core.config import (
    COORDINATES_CSV,
    GEOCODE_CACHE_FILE,
    FUZZY_LOCATION_THRESHOLD,
    USE_LEGACY_GEOCODING,
    USE_NOMINATIM_GEOCODING,
)

# Nominatim requires a descriptive user_agent and respects 1 req/sec
_geolocator = Nominatim(user_agent="dubai_realestate_matcher_v1", timeout=10)
_last_nominatim_call = 0.0   # rate-limit tracker


# ── Coordinates Map ───────────────────────────────────────────────────────────
_coord_map: dict[str, tuple[float, float]] = {}


def _load_coord_map():
    global _coord_map
    _coord_map = {}
    if not os.path.exists(COORDINATES_CSV):
        return

    with open(COORDINATES_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("canonical_name") or "").strip()
            lat_raw = (row.get("lat") or "").strip()
            lng_raw = (row.get("lng") or "").strip()
            if not name or not lat_raw or not lng_raw:
                continue
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
            except ValueError:
                continue
            _coord_map[name.lower()] = (lat, lng)


_load_coord_map()


# ── Cache ──────────────────────────────────────────────────────────────────────
_cache: dict = {}


def _load_cache():
    global _cache
    if os.path.exists(GEOCODE_CACHE_FILE):
        with open(GEOCODE_CACHE_FILE, "r") as f:
            _cache = json.load(f)
    else:
        _cache = {}


def _save_cache():
    with open(GEOCODE_CACHE_FILE, "w") as f:
        json.dump(_cache, f, indent=2)


_load_cache()


# ── Phase-variant guard ────────────────────────────────────────────────────────

def _is_phase_variant(s1: str, s2: str) -> bool:
    """
    Returns True if s1 and s2 are DIFFERENT phases/editions of the same development.
    These are distinct locations and must NEVER be fuzzy-matched.

    Arabian Ranches   vs  Arabian Ranches 3   → True  ← BLOCK
    DAMAC Hills       vs  DAMAC Hills 2        → True  ← BLOCK
    Arabian Ranches 2 vs  Arabian Ranches 3    → True  ← BLOCK
    Business Bay      vs  Businessbay          → False ← allow (typo)
    Dubai Marina      vs  Dubai marinaa        → False ← allow (typo)
    """
    _num_suffix = re.compile(r'^(.*?)\s*(\d+)\s*$')

    m1 = _num_suffix.match(s1.strip().lower())
    m2 = _num_suffix.match(s2.strip().lower())

    base1 = m1.group(1).strip() if m1 else s1.strip().lower()
    base2 = m2.group(1).strip() if m2 else s2.strip().lower()
    num1 = int(m1.group(2)) if m1 else None
    num2 = int(m2.group(2)) if m2 else None

    # Only relevant if the numbers differ (includes None vs number)
    if num1 == num2:
        return False

    # Check if the base names are the same development
    # Use a high threshold here — bases must be very similar to be a phase issue
    base_sim = fuzz.ratio(base1, base2)
    if base_sim >= 82:
        return True   # Same base, different phase number → DIFFERENT locations

    return False


# ── Step 1: Fuzzy typo correction ─────────────────────────────────────────────

def fuzzy_match_known(location: str) -> str | None:
    """
    Find a known cached location that is a TYPO variant of this one.
    Uses fuzz.ratio (length-sensitive) instead of token_sort_ratio.

    Allows:  "Businessbay"  → "business bay"    (typo)
    Blocks:  "Arabian Ranches" → "arabian ranches 3"  (phase variant — different area)
    """
    key = location.strip().lower()
    best_score = 0
    best_match = None

    for cached_key in _cache:
        # Hard block: never fuzzy-match phase variants regardless of score
        if _is_phase_variant(key, cached_key):
            continue

        # fuzz.ratio is length-sensitive — penalises extra tokens like "3"
        score = fuzz.ratio(key, cached_key)
        if score > best_score:
            best_score = score
            best_match = cached_key

    if best_score >= FUZZY_LOCATION_THRESHOLD:
        return best_match
    return None


# ── Step 2: Nominatim geocoding ────────────────────────────────────────────────

def _geocode_raw(query: str) -> tuple[float, float] | None:
    """
    Nominatim (OpenStreetMap) geocoding.
    Enforces 1 request/second as required by Nominatim usage policy.
    Retries once on timeout.
    """
    global _last_nominatim_call

    # Rate-limit: at least 2s between calls
    elapsed = time.time() - _last_nominatim_call
    if elapsed < 2:
        time.sleep(2 - elapsed)

    try:
        _last_nominatim_call = time.time()
        result = _geolocator.geocode(query)
        if result:
            return (result.latitude, result.longitude)
        return None
    except GeocoderTimedOut:
        print(f"[Geocoder] Nominatim timeout for '{query}', retrying...")
        time.sleep(2)
        try:
            result = _geolocator.geocode(query)
            return (result.latitude, result.longitude) if result else None
        except Exception:
            return None
    except GeocoderServiceError as e:
        print(f"[Geocoder] Nominatim service error for '{query}': {e}")
        return None
    except Exception as e:
        print(f"[Geocoder] Unexpected error for '{query}': {e}")
        return None


# ── Main public API ────────────────────────────────────────────────────────────

def geocode(location: str) -> tuple[float, float] | None:
    """
    Full pipeline:
    0. coordinates.csv exact match
    1. Exact cache lookup
    2. Fuzzy typo correction (phase variants blocked)
    3. Nominatim geocoding (with Dubai context + fallback)
    """
    if not location:
        return None

    raw_key = location.strip().lower()

    # 0. coordinates.csv exact match
    if raw_key in _coord_map:
        return _coord_map[raw_key]

    if not USE_LEGACY_GEOCODING:
        return None

    # 1. Exact cache hit
    if raw_key in _cache:
        entry = _cache[raw_key]
        return (entry["lat"], entry["lng"]) if entry else None

    # 2. Fuzzy typo correction against cached keys
    fuzzy_key = fuzzy_match_known(location)
    if fuzzy_key and fuzzy_key in _cache:
        entry = _cache[fuzzy_key]
        if entry:
            print(f"[Geocoder] Fuzzy match: '{location}' → '{fuzzy_key}'")
            _cache[raw_key] = entry
            _save_cache()
            return (entry["lat"], entry["lng"])

    if not USE_NOMINATIM_GEOCODING:
        return None

    # 3. Nominatim — try with Dubai context, then bare name as fallback
    query = location if "dubai" in location.lower() else f"{location}, Dubai, UAE"
    coords = _geocode_raw(query)

    if coords is None and "dubai" not in location.lower():
        coords = _geocode_raw(location)

    if coords:
        entry = {"lat": coords[0], "lng": coords[1], "normalized": location}
        _cache[raw_key] = entry
    else:
        _cache[raw_key] = None
        print(f"[Geocoder] Could not geocode '{location}'")

    _save_cache()
    return coords


# ── Distance ──────────────────────────────────────────────────────────────────

def haversine_km(coord1: tuple, coord2: tuple) -> float:
    """Haversine distance in km between two (lat, lng) tuples."""
    R = 6371.0
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def location_distance_km(loc1: str, loc2: str) -> float | None:
    """Distance in km between two location strings. None if either can't be geocoded."""
    c1 = geocode(loc1)
    c2 = geocode(loc2)
    if c1 is None or c2 is None:
        return None
    return haversine_km(c1, c2)