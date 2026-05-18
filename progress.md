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

## 2. Config Reference (`core/config.py`)

| Constant | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | Gemini API key |
| `API_KEY` | — | API key for FastAPI auth |
| `MONGO_URI` | — | MongoDB connection string (Atlas or local) |
| `MONGO_DB_NAME` | `"realestate"` | Database name |
| `PRICE_TOLERANCE` | `0.10` | ±10% price flexibility for matching |
| `DISTANCE_KM_TOLERANCE` | `2.0` | Max km between buyer and seller location |
| `BHK_TOLERANCE` | `0` | 0 = exact bedroom match; 1 = allow ±1 |
| `SQFT_TOLERANCE` | `0.15` | ±15% sqft tolerance; `None` = disable |
| `MIN_MATCH_SCORE` | `0.75` | Minimum weighted score to record a match |
| `REQUIRE_PRICE_OR_LOCATION` | `True` | Reject if both price and location are skipped |
| `FUZZY_LOCATION_THRESHOLD` | `85` | Legacy typo threshold in geocoder |
| `LOCATION_FUZZY_THRESHOLD` | `80` | Resolver fuzzy cutoff |
| `DUPLICATE_DETECTION` | `True` | Enable fingerprint-based duplicate rejection |
| `DUPLICATE_PRICE_TOLERANCE` | `0.05` | ±5% for fingerprint price comparison |
| `LOCATIONS_CSV` | `"data/locations.csv"` | Canonical names + aliases |
| `COORDINATES_CSV` | `"data/coordinates.csv"` | canonical_name → lat, lng |
| `LOCATION_RESOLUTION_CACHE_FILE` | `"cache/location_cache.json"` | Resolver output cache |
| `UNRESOLVED_LOG_FILE` | `"cache/unresolved_locations.log"` | Failed resolution log |
| `GEOCODE_CACHE_FILE` | `"cache/geocode_cache.json"` | Legacy Nominatim cache |
| `USE_LEGACY_GEOCODING` | `False` | If False, only data/coordinates.csv is used |
| `USE_NOMINATIM_GEOCODING` | `True` | Enables Nominatim fallback when legacy geocoding is on |
| `USE_GEMINI_PARSER_CLASSIFIER` | `False` | Toggle Gemini classifier |
| `USE_GEMINI_PARSER_TEXT_EXTRACTION` | `True` | Toggle Gemini text extraction |
| `USE_GEMINI_PARSER_IMAGE_EXTRACTION` | `True` | Toggle Gemini image extraction |
| `USE_GEMINI_RESOLVER_DISAMBIGUATE` | `True` | Toggle Gemini disambiguation |
| `USE_GEMINI_RESOLVER_CONFIRM` | `True` | Toggle Gemini confirmation |
| `USE_GEMINI_RESOLVER_COLD_STEP_A` | `True` | Toggle Gemini cold Step A |
| `USE_GEMINI_RESOLVER_COLD_STEP_B` | `True` | Toggle Gemini cold Step B |
| `DELETE_AFTER_MATCH` | `False` | `True` = delete matched docs from buy/sell |
| `MODEL` | `"gemini-2.5-flash-lite"` | Gemini model used by parser and resolver |

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
- **Parser** (`ingestion/parser.py`): Uses Gemini `gemini-2.5-flash` with `response_mime_type: application/json` to extract structured listing data from both text messages and image flyers. Handles multiple listings per message, buyer vs seller detection, broker contact extraction.
- **Geocoder** (`location/geocoder.py`): 4-layer pipeline — exact cache → fuzzy typo correction → Gemini location normalization → Nominatim geocoding. Results cached in `cache/geocode_cache.json`.
- **Database** (`core/database.py`): MongoDB with two listing collections (`buy_listings`, `sell_listings`) and a `matches` collection. Compound indexes on type/BHK/price for efficient pre-filtering. MD5 fingerprint-based duplicate detection.
- **Matcher** (`core/matcher.py`): Weighted scoring engine. MongoDB pre-filters candidates. Python-side checks: property type, BHK, price, sqft, location (Haversine). Two quality gates: `REQUIRE_PRICE_OR_LOCATION` and `MIN_MATCH_SCORE`.
- **CLI** (`main.py`): `--text`, `--image`, `--raw`, `--match-only`, `--stats`, `--show-matches`, `--dedupe`, `--clear`.
- **Config** (`core/config.py`): Single source of truth for all constants and tolerances.

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
5. API keys and MongoDB URI hardcoded in `core/config.py` — needs `.env` migration

---

## What Copilot Should Work On Next

Sessions will be driven by prompts from Melvi. Each prompt comes from a planning session with Claude. After each session, add a new dated entry below following the template in `copilot-instructions.md`.

---

<!-- Copilot: Add new entries below this line after each coding session -->

## 2026-05-18 — Refactor structure and paths

### What was done
- Updated imports to new module paths under core/, ingestion/, location/, and tools/
- Centralized data/cache paths using `_PROJECT_ROOT` in core/config.py
- Added package `__init__.py` files and cache/.gitkeep; tightened .gitignore to cache-only artifacts
- Updated tools usage strings and documentation for new layout

### Files changed
- core/config.py, core/database.py, core/matcher.py
- ingestion/parser.py, ingestion/api.py
- location/geocoder.py, location/resolver.py
- tools/convert_excel_to_csv.py, tools/geocode_locations.py
- main.py, README.md, CONTEXT.md, progress.md, .gitignore
- core/__init__.py, ingestion/__init__.py, location/__init__.py, tools/__init__.py, cache/.gitkeep

### Decisions made
- Keep main.py at repo root to preserve CLI and `uvicorn main:app`
- Use Path-based project root to avoid brittle relative paths

### What was learned
- Absolute data/cache paths reduce tool execution surprises across directories

### Next step
- Run a quick CLI/API smoke test to confirm imports and paths

## 2026-05-18 — API usage toggles and legacy geocoding gate

### What was done
- Added per-call toggles for Gemini and Nominatim API usage
- Added data/coordinates.csv Layer 0 geocoding with optional legacy fallback

### Files changed
- core/config.py — per-call API toggles and legacy geocoding switch
- location/geocoder.py — data/coordinates.csv map + legacy/Nominatim gating
- ingestion/parser.py — separate Gemini toggles for classifier, text, and image extraction
- location/resolver.py — per-mode Gemini toggles for disambiguate/confirm/cold
- CONTEXT.md — updated system docs
- progress.md — session log

### Decisions made
- Make every external API call individually switchable to control cost and quota
- Keep data/coordinates.csv as primary geocoding source, with legacy fallback behind a toggle

### What was learned
- Granular toggles make it easier to isolate API cost spikes during batch runs

### Next step
- Validate toggle combinations in a dry run and confirm expected fallbacks

## 2026-05-17 — Location resolver overhaul + batch processing fixes

### What was done
- Rewrote location/resolver.py from 3-layer to 4-layer pipeline with candidate generation and confidence scoring
- Added phase-aware matching — `ar3` now correctly resolves to Arabian Ranches 3, not Arabian Ranches
- Added token coverage scoring — sub-communities (Marina Gate) no longer collapse into parent areas (Dubai Marina)
- Added three targeted Gemini modes: Disambiguate, Confirm, Cold — replacing blind two-step call
- Fixed batch processing in main.py — messages now parsed and inserted one at a time instead of all-at-once
- Added rate limit retry logic with configurable wait and max retries
- Added inter-message delay (`_MESSAGE_DELAY_SECONDS = 13`) to stay under Gemini free tier RPM limit
- Added auto separator detection for messages.txt (supports ---, ===, ***, ~~~, double blank lines)
- Cleaned up CLI output — only errors and final summary printed during batch runs

### Files changed
- `location/resolver.py` — full rewrite of Layer 1+2, new candidate scoring system, 3-mode Gemini
- `main.py` — `_process_text_file()`, `_split_messages()`, `_parse_with_retry()` added

### Decisions made
- Confidence = 0.50×fuzzy + 0.35×token_coverage + 0.15×phase_score
- Phase hard-block: input with phase X never matches candidate with different or no phase
- Single-token inputs get neutral 0.50 token coverage to avoid penalizing abbreviations
- Decision gate thresholds: HIGH=0.82, AMBIGUITY_BAND=0.08, CONFIRM=0.65
- Gemini only called when Layer 2 is uncertain — reduces API usage significantly
- Per-message insert means partial progress is always saved on crash or quota hit

### What was learned
- Free tier daily cap is 20 requests — insufficient for 183-message batch runs; paid tier needed for production
- Each message uses 2+ Gemini calls (classifier + parser + optional location resolver escalations)
- Changing MODEL in core/config.py only takes effect on next process start — in-memory model instance is not hot-reloaded

### Known issues
- `data/locations.csv` needs phased communities added as separate canonical rows (Arabian Ranches 2/3 etc.) for L1 exact match to work on abbreviations like `ar3`
- `DUPLICATE_PRICE_TOLERANCE` still defined in config but not wired into `_build_fingerprint()`
- Free tier quota (20 req/day) is a hard blocker for batch runs — need paid Gemini API access

### Next step
- Add phased communities to data/locations.csv
- Upgrade Gemini API to paid tier for production batch runs
- Test with real broker messages and review cache/unresolved_locations.log

## 2026-05-14 — Location resolution system

### What was done
- Built a 3-layer location resolver (alias lookup, fuzzy match, Gemini constrained list)
- Added Excel-to-CSV converter for maintaining canonical locations
- Wired canonical resolution into listing normalization

### Files changed
- location/resolver.py — new resolver with cache, Gemini fallback, and unresolved logging
- data/locations.csv — initial canonical locations and aliases
- tools/convert_excel_to_csv.py — Excel conversion script
- core/config.py — location resolution settings and thresholds
- location/geocoder.py — removed Gemini normalization step
- ingestion/parser.py — resolve raw locations to canonical names
- requirements.txt — add openpyxl
- README.md — document location resolution workflow
- progress.md — session log

### Decisions made
- Use WRatio for fuzzy matching to handle abbreviations and concatenations better than ratio
- Include canonical names in the Layer 1 fuzzy keyspace to catch direct matches and typos
- Constrain Gemini to the canonical list to prevent invented locations
- Log the best fuzzy guess to guide threshold tuning and alias expansion

### What was learned
- The unresolved log provides actionable signals for improving the alias sheet and thresholds

### Next step
- Test with real broker messages and review cache/unresolved_locations.log

## 2026-05-13 — make.com integration

### What was done
- Integrated the system with make.com to call the API remotely and accept inputs from services like WhatsApp and Gmail

### Files changed
- progress.md — session log

### Decisions made
- Use make.com as the orchestration layer for external message sources

### What was learned
- Centralizing ingestion through the API enables multi-channel inputs without code changes per channel

### Next step
- Verify make.com scenarios for WhatsApp and Gmail are sending requests with X-API-Key

## 2026-05-13 — Hybrid message classifier

### What was done
- Added hybrid rules + Gemini classification for message filtering

### Files changed
- ingestion/parser.py — rule scoring and JSON-based Gemini classifier with confidence gating
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
- core/config.py — load API_KEY from environment
- ingestion/parser.py — add is_real_estate_message() filter
- ingestion/api.py — new FastAPI app with ingest/stats/matches endpoints
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
- core/config.py — load .env and read GEMINI_API_KEY/MONGO_URI from environment
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