# Project Progress — Real Estate WhatsApp Matcher

> **What this file is:** A running log of everything built, every decision made, and everything learned throughout this project. Maintained by GitHub Copilot after every coding session. Full history is always preserved — nothing is deleted or compressed.

---
### References
## 1. MongoDB Schema

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

## 2. Config Reference (`config.py`)

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


## 3. CLI Usage

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

## Project Metadata

- **Builder:** Melvin
- **Planning:** Melvin + Claude (Anthropic)
- **Coding Agent:** GitHub Copilot
- **Domain:** Dubai real estate — WhatsApp message parsing + buyer/seller matching
- **Stack:** Python, Gemini API, MongoDB Atlas, Nominatim (OpenStreetMap), geopy, rapidfuzz

---

## Phase 1 — Foundation (Pre-Copilot, designed with Claude)

### What was built
The entire base system was designed and implemented through a planning + coding session with Claude. Copilot is taking over from this point. Everything below is the established history before Copilot's first session.

### Core architecture designed
- **Parser** (`parser.py`): Uses Gemini `gemini-2.5-flash` with `response_mime_type: application/json` to extract structured listing data from both text messages and image flyers. Handles multiple listings per message, buyer vs seller detection, broker contact extraction.
- **Geocoder** (`geocoder.py`): 4-layer pipeline — exact cache → fuzzy typo correction → Gemini location normalization → Nominatim geocoding. Results cached in `geocode_cache.json`.
- **Database** (`database.py`): MongoDB with two listing collections (`buy_listings`, `sell_listings`) and a `matches` collection. Compound indexes on type/BHK/price for efficient pre-filtering. MD5 fingerprint-based duplicate detection.
- **Matcher** (`matcher.py`): Weighted scoring engine. MongoDB pre-filters candidates. Python-side checks: property type, BHK, price, sqft, location (Haversine). Two quality gates: `REQUIRE_PRICE_OR_LOCATION` and `MIN_MATCH_SCORE`.
- **CLI** (`main.py`): `--text`, `--image`, `--raw`, `--match-only`, `--stats`, `--show-matches`, `--dedupe`, `--clear`.
- **Config** (`config.py`): Single source of truth for all constants and tolerances.

### Bugs found and fixed during design phase

**Price matching bug (Critical)**
- Problem: Buyer with 4.5M budget was matching seller asking 8.7M
- Root cause: Upper price bound was accidentally set to infinity when only `price_min_aed` was present
- Fix: Single buyer budget is now treated as MAX budget. Range = `[budget × 0.9, budget × 1.1]`. No sell above max is ever matched.

**Geocoding failures**
- Problem: Location names like "Greenway", "Costa Brava", "Majan" failing to geocode
- Root cause: Nominatim needs official area names; brokers use project/community shorthand
- Fix: Gemini normalization step added before geocoding. Converts broker slang → official names. No static alias map (unmaintainable; Dubai project names change constantly).

**Arabian Ranches ≠ Arabian Ranches 3**
- Problem: Fuzzy matcher was equating phase variants of the same development as the same location
- Root cause: `fuzz.token_sort_ratio` was used — it sorts tokens before comparing, so the "3" barely penalised the score
- Fix 1: Switched to `fuzz.ratio` (length-sensitive)
- Fix 2: Added `_is_phase_variant()` hard guard — strips trailing numbers from both strings; if bases are similar (≥82%) but numbers differ, they are BLOCKED from fuzzy matching regardless of score
- Tested: 6 unit test cases all pass

**Weak matches accepted**
- Problem: 25-60% score matches (e.g. type match only, everything else skipped) were being recorded
- Fix: Two gates — `REQUIRE_PRICE_OR_LOCATION=True` (at least one must actually match, not just skip) and `MIN_MATCH_SCORE=0.75`

**Duplicate accumulation**
- Problem: 82 buyers + 95 sellers accumulated from repeated test runs
- Root cause: Fingerprint detection was added after initial data insertion; old docs had no fingerprint field
- Fix: `_build_fingerprint()` (MD5 of key fields, price bucketed to ±50K), `_is_duplicate()` on every insert, `dedupe_collection()` for retroactive cleanup
- Command: `python main.py --dedupe`

**Historical match re-recording**
- Problem: Re-running `--match-only` would record same match twice
- Fix: `already_matched_pair()` checked before every `record_match()`. Unique compound index `(buy_id, sell_id)` on matches collection as DB-level safety net.

### Key design decisions

| Decision | Why |
|---|---|
| Gemini for parsing | Multimodal (text + images in one API), forced JSON output, generous free tier |
| Nominatim over Google Maps | Free, no API key required |
| Gemini for location normalization | Dubai has hundreds of project names, constantly growing. Static maps are unmaintainable. |
| `fuzz.ratio` over `token_sort_ratio` | Length-sensitive — critical for distinguishing "Arabian Ranches" from "Arabian Ranches 3" |
| MD5 fingerprint for deduplication | Tolerates minor reprice reposts (±50K bucket) and sqft rounding |
| MongoDB pre-filtering by type+BHK | Reduces geocoding calls, which are the real bottleneck (1 req/sec Nominatim rate limit) |
| `MIN_MATCH_SCORE = 0.75` | 0.50 and 0.60 allowed too many junk matches in testing |
| `response_mime_type: application/json` | Forces Gemini to output valid JSON — eliminates markdown fence stripping errors |

### Known issues at handoff to Copilot

1. `DUPLICATE_PRICE_TOLERANCE` in config imported but not wired into `_build_fingerprint()` — currently hardcodes 50000
2. `FUZZY_LOCATION_THRESHOLD=85` may be slightly permissive — monitor in production
3. Matching is 1-to-1; multi-match (top 3 sellers per buyer) not yet implemented
4. No WhatsApp automation yet — CLI only
5. API keys and MongoDB URI hardcoded in `config.py` — needs `.env` migration

---

## What Copilot Should Work On Next

Sessions will be driven by prompts from Melvi. Each prompt comes from a planning session with Claude. After each session, add a new dated entry below following the template in `copilot-instructions.md`.

---

<!-- Copilot: Add new entries below this line after each coding session -->

## 2026-05-13 — Hybrid message classifier

### What was done
- Added hybrid rules + Gemini classification for message filtering

### Files changed
- parser.py — rule scoring and JSON-based Gemini classifier with confidence gating
- progress.md — session log

### Decisions made
- Only filter out messages when Gemini says "no" with high confidence and rules are weak

### What was learned
- Combining deterministic signals with model confidence reduces false negatives

### Next step
- Validate with a labeled sample set and tune thresholds

## 2026-05-12 — Add FastAPI ingestion server

### What was done
- Added FastAPI server with text and image ingest endpoints and API key auth
- Implemented Gemini-based message filtering for text ingestion
- Exposed API app via main.py for uvicorn
- Added server dependencies and API_KEY env variable support

### Files changed
- requirements.txt — add fastapi, uvicorn, python-multipart
- .env.example — add API_KEY placeholder
- config.py — load API_KEY from environment
- parser.py — add is_real_estate_message() filter
- api.py — new FastAPI app with ingest/stats/matches endpoints
- main.py — document API run commands and expose app
- progress.md — session log

### Decisions made
- Use a simple X-API-Key header middleware for auth
- Fail open on Gemini filter errors to avoid dropping messages
- Include broker contacts in match responses via stored snapshots

### What was learned
- Reusing match snapshots keeps API responses consistent with stored matches

### Next step
- Run the API locally with uvicorn and test /ingest/text and /ingest/image

## 2026-05-12 — Move secrets to .env

### What was done
- Added dotenv loading and switched secrets to environment variables
- Added .env.example with placeholder keys
- Added .env to .gitignore
- Added python-dotenv dependency

### Files changed
- config.py — load .env and read GEMINI_API_KEY/MONGO_URI from environment
- requirements.txt — add python-dotenv
- .env.example — placeholder environment variables
- .gitignore — ignore .env
- progress.md — session log

### Decisions made
- Keep secrets out of source by relying on python-dotenv in config

### What was learned
- Centralizing load_dotenv in config makes env usage consistent across modules

### Next step
- Create a local .env from .env.example and fill in real keys