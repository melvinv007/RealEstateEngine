# Real Estate WhatsApp/Gmail (or any other) Matcher — Full Project Context

> **Purpose of this document:** Complete context transfer for any agent, developer, or LLM picking up this project. Covers what we built, every architectural decision made, every bug fixed and why, and the current state of each file.

---

## 1. What This Project Does

Dubai real estate brokers share property listings and buyer requirements in WhatsApp/Gmail (or any other) chats — as raw text messages or image flyers. The volume is high and matching buyers to sellers manually is slow and error-prone.

This system:
1. **Ingests** WhatsApp/Gmail/Telegram (or any other) messages (text dumps or image flyers)
2. **Parses** them using Gemini AI into structured JSON listings
3. **Stores** them in MongoDB in two collections: `buy_listings` and `sell_listings`
4. **Matches** buyers to sellers automatically using configurable tolerances
5. **Records** matches in a `matches` collection with a score and reasons

---

## 2. File Structure

```
RealEstate/
├── config.py          ← All tunable constants (single source of truth)
├── parser.py          ← Gemini-powered text + image parser
├── geocoder.py        ← Location normalization + Nominatim geocoding
├── database.py        ← MongoDB CRUD, duplicate detection, indexes
├── matcher.py         ← Buy/sell matching engine
├── main.py            ← CLI entry point
├── requirements.txt
└── geocode_cache.json ← Auto-created, persists geocoded coordinates
```

---

## 3. Tech Stack & Why

| Component | Choice | Why |
|---|---|---|
| **LLM Parser** | Google Gemini (`gemini-2.5-flash`) | Multimodal — handles both text and image flyers in one API. Forced JSON output via `response_mime_type`. |
| **Geocoding** | Nominatim (OpenStreetMap) via `geopy` | Free, no API key. Google Maps Geocoding API was the original choice but is paid. |
| **Location normalization** | Gemini (same API key) | Broker messages use project names, tower names, abbreviations that Nominatim can't geocode directly. Gemini converts "Costa Brava" → "DAMAC Lagoons", "JVC" → "Jumeirah Village Circle". Static alias maps were rejected because Dubai project names change constantly and the list would never be complete. |
| **Fuzzy matching** | `rapidfuzz` (`fuzz.ratio`) | Catches typos like "Businessbay" → "Business Bay" before geocoding. Switched from `token_sort_ratio` to `ratio` because `token_sort_ratio` was too aggressive (see Section 6). |
| **Database** | MongoDB Atlas | Chosen over SQLite for scalability, cloud access, and native JSON document storage. |
| **Distance** | Haversine formula | Standard great-circle distance, accurate enough for city-scale (Dubai). |

---

## 4. Data Flow

```
Input (text file / image / raw string)
        │
        ▼
    parser.py
    Gemini API → structured JSON array
    Each listing: transaction, type, location, price, BHK, sqft, broker, etc.
        │
        ▼
    database.py  insert_listing()
    ├── Build fingerprint (MD5 of type+location+bhk+price_bucket+sqft)
    ├── Check for duplicate → reject if exists
    ├── Route to buy_listings or sell_listings based on transaction field
    └── Store with matched=False, match_id=None
        │
        ▼
    matcher.py  run_matching()
    For each unmatched buy:
    ├── MongoDB pre-filter by property_type + BHK (indexed)
    ├── For each sell candidate:
    │   ├── property_type check (hard fail)
    │   ├── BHK check (hard fail if BHK_TOLERANCE=0)
    │   ├── price check (hard fail if out of range)
    │   ├── sqft check (soft, skippable)
    │   └── location check → fuzzy → geocode → Haversine
    ├── Score passed checks
    ├── Apply quality gates (MIN_MATCH_SCORE, REQUIRE_PRICE_OR_LOCATION)
    └── Pick best scoring sell for this buy
        │
        ▼
    database.py  record_match()
    ├── Check historical match (don't re-record same pair)
    ├── Insert into matches collection
    └── Mark buy + sell as matched=True
```

---

## 5. MongoDB Schema

### buy_listings / sell_listings

```json
{
  "transaction": "buy" | "sell",
  "property_type": "apartment" | "villa" | "townhouse" | "plot" | "warehouse" | "duplex" | "penthouse" | "studio" | "office" | "other",
  "location": "Business Bay",
  "price_aed": 2500000,
  "price_min_aed": null,
  "price_max_aed": null,
  "price_per_sqft_aed": null,
  "bhk": 2,
  "sqft": 1200,
  "plot_sqft": null,
  "is_ready": true,
  "handover_year": null,
  "payment_plan": "60% paid, 30% construction, 10% handover",
  "furnishing": null,
  "amenities": ["canal view", "parking"],
  "is_distress": false,
  "is_mortgage": false,
  "is_cash": false,
  "broker": {
    "name": "Ahmed",
    "phone": "+971501234567",
    "company": "XYZ Real Estate"
  },
  "notes": "Brand new, vacant",
  "raw_text": "...",
  "fingerprint": "md5hash",
  "created_at": "2025-01-01T00:00:00Z",
  "matched": false,
  "match_id": null
}
```

### matches

```json
{
  "buy_id": "ObjectId",
  "sell_id": "ObjectId",
  "match_score": 0.85,
  "match_reasons": [
    "type_match(apartment)",
    "bhk_match(2BR)",
    "price_match(sell=2500000 in [2250000–2750000])",
    "location_match(dist=0.8km ≤ 2.0km)"
  ],
  "buy_snapshot": { ...full buy doc... },
  "sell_snapshot": { ...full sell doc... },
  "matched_at": "2025-01-01T00:00:00Z"
}
```

### MongoDB Indexes (auto-created on first connection)

| Collection | Index | Purpose |
|---|---|---|
| buy/sell | `(matched, property_type)` | Pre-filter unmatched by type |
| buy/sell | `(matched, bhk)` | Pre-filter by bedroom count |
| buy/sell | `(matched, price_aed)` | Pre-filter by price band |
| buy/sell | `fingerprint` | Duplicate detection |
| matches | `(buy_id, sell_id)` unique | Prevent duplicate match records |

---

## 6. Bugs Fixed and Why — Full History

### 6.1 Price Matching Bug (Critical)
**Problem:** A buyer with budget 4.5M was matching with a seller asking 8.7M.

**Root cause:** The original code set `effective_min = buy_price * (1 - TOL)` but `effective_max = infinity` when only `price_min_aed` was present and `price_max_aed` was None. Result: the upper bound was unbounded.

**Fix:** A single buyer budget is now treated as **maximum budget**, not a midpoint:
```
buyer budget = 4.5M, PRICE_TOLERANCE = 0.10
→ effective range: [4.05M, 4.95M]
```
Sell price must be strictly within this range. No value above 4.95M can ever match.

---

### 6.2 Geocoding Failures
**Problem:** Many Dubai locations failed geocoding silently:
- "Greenway" (a project name, not an area)
- "Costa Brava" (DAMAC Lagoons sub-community)
- "Majan" (informal name for part of Dubailand)

**Root cause:** Nominatim (and Google Maps) work with official area names. Broker messages use project names, community sub-names, and abbreviations that geocoders don't recognise.

**Fix:** Two-layer normalization before geocoding:

1. **Fuzzy match** against already-cached location keys — catches typos ("Businessbay" → "Business Bay") without any API call.
2. **Gemini normalization** — converts project/community names to official geocodable names. Uses the same Gemini API key already in config. Result is cached in-memory per session so Gemini is called at most once per unique location string.

**Why not a static alias map?**
Dubai project names are created constantly. A static map requires manual maintenance and is always incomplete. Gemini handles any current or future name automatically.

---

### 6.3 Arabian Ranches ≠ Arabian Ranches 3
**Problem:** The fuzzy matcher was equating "Arabian Ranches" with "Arabian Ranches 3" — these are geographically separate communities.

**Root cause:** We were using `fuzz.token_sort_ratio`, which sorts tokens alphabetically before comparing. With this method, "arabian ranches" vs "arabian ranches 3" scored ~95% because "3" is just one extra token with low weight.

**Fix:** Two-part solution:

1. **Switched to `fuzz.ratio`** — length-sensitive, penalises extra tokens meaningfully.
2. **Added `_is_phase_variant()` guard** — a hard pre-check that runs before any fuzzy score:
   - Strips trailing numbers from both strings
   - If the base names are similar (≥82%) but the trailing numbers differ (including None vs a number), they are DIFFERENT locations — hard blocked from fuzzy matching regardless of score.
   - Examples:
     - "Arabian Ranches" vs "Arabian Ranches 3" → **BLOCKED** (same base, one has "3")
     - "DAMAC Hills" vs "DAMAC Hills 2" → **BLOCKED**
     - "Arabian Ranches 2" vs "Arabian Ranches 3" → **BLOCKED** (different numbers)
     - "Businessbay" vs "Business Bay" → **ALLOWED** (no numbers, typo only)
     - "Dubai Marina" vs "Dubai marinaa" → **ALLOWED** (typo only)

**Unit tested:** All 6 cases pass.

**Note for Gemini normalization:** The prompt explicitly instructs Gemini to preserve phase numbers. "Arabian Ranches 3" must not be normalized to "Arabian Ranches".

---

### 6.4 Weak Matches Being Accepted
**Problem:** Matches with 25-60% score were being recorded — often just type matching with everything else skipped.

**Fix:** Two gates added:
1. `REQUIRE_PRICE_OR_LOCATION = True` — at least one of price or location must have *actually matched* (not just been skipped due to missing data). Pure type+BHK matches with no price or location evidence are rejected.
2. `MIN_MATCH_SCORE = 0.75` — minimum weighted score. Scores below 75% are discarded entirely.

**Score weights:**
| Field | Weight |
|---|---|
| price_match | 0.35 |
| type_match | 0.25 |
| location_exact_match | 0.20 |
| location_match (by distance) | 0.15 |
| bhk_match | 0.15 |
| sqft_match | 0.10 |

---

### 6.5 Duplicate Accumulation
**Problem:** Repeated test runs with the same messages produced 82 buyer and 95 seller documents in MongoDB (all duplicates).

**Root cause:** Fingerprint detection was added after the initial data was already inserted, so the fingerprint field was missing on existing documents.

**Fix:**
1. **`_build_fingerprint()`** — MD5 hash of `{type, location, bhk, price_bucket, sqft, transaction}`. Price is bucketed to nearest DUPLICATE_PRICE_TOLERANCE to tolerate minor reprice reposts.
2. **`_is_duplicate()`** — checks fingerprint before every insert.
3. **`dedupe_collection()`** — retroactive deduplication: scans all existing docs, keeps oldest per fingerprint, deletes the rest, backfills the fingerprint field on survivors.

**Run once to clean existing data:**
```bash
python main.py --dedupe
```

---

### 6.6 Historical Match Prevention
**Problem:** Re-running `--match-only` would re-match and re-record the same buy/sell pairs.

**Fix:** `already_matched_pair(buy_id, sell_id)` checks the `matches` collection before recording. The `matches` collection also has a unique compound index on `(buy_id, sell_id)` as a database-level safety net.

---

### 6.7 Algorithm Efficiency
**Question:** Is O(n×m) brute force acceptable?

**Answer:** Yes for this use case (broker groups rarely exceed a few thousand listings). The real bottleneck is geocoding API calls (each takes 1+ second due to Nominatim's rate limit).

**Optimisation applied:** MongoDB compound indexes on `property_type` and `bhk` allow `get_unmatched_filtered()` to return only type+BHK-matching candidates. This reduces the Python-side comparison set and — more importantly — reduces the number of geocoding calls triggered.

---

### 6.8 Nominatim Rate Limiting
**Problem:** Nominatim (OpenStreetMap) has a strict 1 request/second policy. Violating it results in IP bans.

**Fix:** `_geocode_raw()` tracks `_last_nominatim_call` timestamp and sleeps for `max(0, 2 - elapsed)` before each call. Also retries once on `GeocoderTimedOut`.

---

## 7. Config Reference (`config.py`)

| Constant | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | Gemini API key |
| `MONGO_URI` | — | MongoDB connection string (Atlas or local) |
| `MONGO_DB_NAME` | `"realestate"` | Database name |
| `PRICE_TOLERANCE` | `0.10` | ±10% price flexibility for matching |
| `DISTANCE_KM_TOLERANCE` | `2.0` | Max km between buyer and seller location |
| `BHK_TOLERANCE` | `0` | 0 = exact bedroom match; 1 = allow ±1 |
| `SQFT_TOLERANCE` | `0.15` | ±15% sqft tolerance; `None` = disable |
| `MIN_MATCH_SCORE` | `0.75` | Minimum weighted score to record a match |
| `REQUIRE_PRICE_OR_LOCATION` | `True` | Reject if both price and location are skipped |
| `FUZZY_LOCATION_THRESHOLD` | `85` | rapidfuzz similarity needed for typo match |
| `DUPLICATE_DETECTION` | `True` | Enable fingerprint-based duplicate rejection |
| `DUPLICATE_PRICE_TOLERANCE` | `0.02` | *(imported but not yet wired up — see known issues)* |
| `USE_GEMINI_LOCATION_NORMALIZER` | `True` | Use Gemini to normalize broker location names |
| `GEOCODE_CACHE_FILE` | `"geocode_cache.json"` | Local cache for geocoded coordinates |
| `DELETE_AFTER_MATCH` | `False` | `True` = delete matched docs from buy/sell |
| `MODEL` | `"gemini-2.5-flash"` | Gemini model used by parser and geocoder |

---

## 8. CLI Usage

```bash
# Parse a text file of WhatsApp/Gmail (or any other) messages
python main.py --text messages.txt

# Parse an image flyer
python main.py --image flyer.png

# Parse inline raw text
python main.py --raw "FOR SALE | Business Bay | 2BR | AED 2.5M"

# Match only (no new input)
python main.py --match-only

# DB stats
python main.py --stats

# View all recorded matches
python main.py --show-matches

# Retroactive duplicate removal
python main.py --dedupe

# ⚠️ Wipe all data
python main.py --clear
```

---

## 9. Known Issues / Open Items

### 9.1 Security: Hardcoded credentials in config.py
API key and MongoDB URI are plaintext in source. Safe for local use. Before any version control sharing, move to `.env` + `python-dotenv`.

### 9.2 Matching is 1-to-1 (best match only)
Currently each buy gets matched to at most one sell. If multiple sells match a buyer equally well, only the highest scorer is recorded. No multiple-match output yet.

---

## 10. Installation

```bash
pip install -r requirements.txt
```

**requirements.txt:**
```
google-generativeai>=0.7.0
geopy
pymongo>=4.6.0
rapidfuzz>=3.6.0
```

---

## 11. Decision Log

| Decision | Alternatives Considered | Reason Chosen |
|---|---|---|
| Gemini for parsing | OpenAI GPT-4o, local LLMs | Already in use, multimodal (text + image), generous free tier |
| Nominatim for geocoding | Google Maps API (paid) | Free, no key required, sufficient accuracy for Dubai areas |
| Gemini for location normalization | Static alias dict | Dubai has hundreds of community/project names and grows constantly; static maps are unmaintainable |
| `fuzz.ratio` over `token_sort_ratio` | `token_sort_ratio`, `partial_ratio` | `ratio` is length-sensitive, critical for distinguishing "Arabian Ranches" from "Arabian Ranches 3" |
| MD5 fingerprint for deduplication | Full-field exact match | Tolerates minor reprice reposts (±50K bucket) and sqft rounding |
| MongoDB indexes on type+BHK+price | Full collection scans | Reduces geocoding call volume, which is the actual bottleneck (1 req/sec rate limit) |
| `MIN_MATCH_SCORE = 0.75` | 0.50, 0.60 | 0.50 and 0.60 allowed too many junk matches in testing |
| `response_mime_type: application/json` in parser | Post-processing JSON extraction | Forces Gemini to output valid JSON directly, eliminates markdown fence stripping errors |