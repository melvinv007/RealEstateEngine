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
├── core/
│   ├── config.py              ← All tunable constants (single source of truth)
│   ├── database.py            ← MongoDB CRUD, duplicate detection, indexes
│   └── matcher.py             ← Buy/sell matching engine
├── ingestion/
│   ├── api.py                 ← FastAPI server for make.com integration
│   └── parser.py              ← Gemini-powered text + image parser
├── location/
│   ├── resolver.py            ← 4-layer location resolution pipeline
│   └── geocoder.py            ← Haversine distance via data/coordinates.csv (optional legacy Nominatim)
├── tools/
│   ├── convert_excel_to_csv.py ← Manual script: converts Excel alias sheet to data/locations.csv
│   └── geocode_locations.py   ← Geocode Excel sheet and sync data/coordinates.csv
├── data/
│   ├── locations.csv          ← Canonical Dubai area names + aliases (~28 areas, growing)
│   └── coordinates.csv        ← canonical_name → lat, lng (partially filled)
├── cache/
│   ├── location_cache.json    ← Auto-created: caches all resolver outputs (pass + fail)
│   ├── geocode_cache.json     ← Auto-created: legacy Nominatim cache (being phased out)
│   └── unresolved_locations.log ← Every location that failed all resolver layers
├── main.py                    ← CLI entry point
├── requirements.txt
├── .env                       ← GEMINI_API_KEY, MONGO_URI, API_KEY (never committed)
└── .env.example               ← Template with placeholder values (committed)
```

---

## 3. Tech Stack & Why

| Component | Choice | Why |
|---|---|---|
| **LLM Parser** | Google Gemini (`gemini-2.5-flash-lite`) | Multimodal — handles both text and image flyers in one API. Forced JSON output via `response_mime_type`. |
| **Location resolution** | 4-layer pipeline (see Section 10) | Broker messages use abbreviations, typos, project names, phase numbers — no single method handles all cases |
| **Geocoding** | `data/coordinates.csv` + Haversine (legacy Nominatim optional) | Coordinates CSV is primary; legacy Nominatim can be enabled for fallback on missing coords. |
| **Fuzzy matching** | `rapidfuzz` (`WRatio`) | Handles abbreviations, concatenations, token reordering better than `ratio` alone |
| **Database** | MongoDB Atlas | Scalability, cloud access, native JSON document storage |
| **Distance** | Haversine formula | Standard great-circle distance, accurate enough for city-scale (Dubai) |
| **API layer** | FastAPI | Thin wrapper for make.com integration. X-API-Key auth. |

---

## 4. Data Flow

```
Input (text file / image / raw string)
        │
        ▼
    ingestion/parser.py
    Gemini API → structured JSON array
    Each listing: transaction, type, location (raw, as-is), price, BHK, sqft, broker, etc.
        │
        ▼
    location/resolver.py  resolve_location(raw_loc)
    4-layer pipeline → canonical name or None
    Sets location_unresolved=True if None (listing still stored)
        │
        ▼
    core/database.py  insert_listing()
    ├── Build fingerprint (MD5 of type+location+bhk+price_bucket+sqft)
    ├── Check for duplicate → reject if exists
    ├── Route to buy_listings or sell_listings based on transaction field
    └── Store with matched=False, match_id=None
        │
        ▼
    core/matcher.py  run_matching()
    For each unmatched buy:
    ├── MongoDB pre-filter by property_type + BHK (indexed)
    ├── For each sell candidate:
    │   ├── property_type check (hard fail)
    │   ├── BHK check (hard fail if BHK_TOLERANCE=0)
    │   ├── price check (hard fail if out of range)
    │   ├── sqft check (soft, skippable)
    │   └── location check → exact string → Haversine via data/coordinates.csv (legacy Nominatim optional)
    ├── Score passed checks
    ├── Apply quality gates (MIN_MATCH_SCORE, REQUIRE_PRICE_OR_LOCATION)
    └── Pick best scoring sell for this buy
        │
        ▼
    core/database.py  record_match()
    ├── Check historical match (don't re-record same pair)
    ├── Insert into matches collection
    └── Mark buy + sell as matched=True
```

---

## 5. MongoDB Schema

### buy_listings / sell_listings

```json
{
  "transaction": "buy | sell",
  "property_type": "apartment | villa | townhouse | plot | warehouse | duplex | penthouse | studio | office | other",
  "location": "Business Bay",
  "location_raw": "bbay",
  "location_unresolved": false,
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
  "buy_snapshot": { "...full buy doc..." },
  "sell_snapshot": { "...full sell doc..." },
  "buy_broker": { "name": "...", "phone": "..." },
  "sell_broker": { "name": "...", "phone": "..." },
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

**Root cause:** The original code set `effective_min = buy_price * (1 - TOL)` but `effective_max = infinity` when only `price_min_aed` was present and `price_max_aed` was None.

**Fix:** A single buyer budget is now treated as **maximum budget**, not a midpoint:
```
buyer budget = 4.5M, PRICE_TOLERANCE = 0.10
→ effective range: [4.05M, 4.95M]
```
Sell price must be strictly within this range. No value above 4.95M can ever match.

---

### 6.2 Geocoding Failures
**Problem:** Many Dubai locations failed geocoding silently — "Greenway", "Costa Brava", "Majan" are project/community names Nominatim doesn't recognise.

**Fix (original):** Two-layer normalization before geocoding — fuzzy match against cached keys + Gemini normalization to convert broker slang to official names.

**Fix (current):** Primary geocoding uses `data/coordinates.csv` — a local CSV mapping canonical names to lat/lng. Legacy Nominatim can be enabled as a fallback via `USE_LEGACY_GEOCODING` + `USE_NOMINATIM_GEOCODING`.

---

### 6.3 Arabian Ranches ≠ Arabian Ranches 3
**Problem:** Fuzzy matcher equated phase variants of the same development.

**Root cause:** `fuzz.token_sort_ratio` sorts tokens alphabetically before comparing — "3" was barely penalised.

**Original fix:** Switched to `fuzz.ratio` + added `_is_phase_variant()` guard.

**Current fix (superseded):** The entire phase problem is now solved architecturally in `location/resolver.py` Layer 2 via phase-aware candidate scoring — see Section 10. `_is_phase_variant()` is retained but the new system handles all cases including previously-missed ones (`ar3` matching `ar`, `marina gate` collapsing to `Dubai Marina`).

---

### 6.4 Weak Matches Being Accepted
**Problem:** Matches with 25–60% score were being recorded.

**Fix:** Two gates:
1. `REQUIRE_PRICE_OR_LOCATION = True` — at least one must actively match, not just skip
2. `MIN_MATCH_SCORE = 0.75` — hard floor on weighted score

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
**Problem:** Repeated test runs produced 82 buyer + 95 seller duplicate documents.

**Fix:**
1. `_build_fingerprint()` — MD5 of `{type, location, bhk, price_bucket, sqft, transaction}`. Price bucketed to nearest 50K.
2. `_is_duplicate()` — checked before every insert.
3. `dedupe_collection()` — retroactive cleanup: keeps oldest per fingerprint, deletes rest, backfills fingerprint on survivors.

```bash
python main.py --dedupe
```

---

### 6.6 Historical Match Prevention
**Problem:** Re-running `--match-only` re-recorded same buy/sell pairs.

**Fix:** `already_matched_pair(buy_id, sell_id)` checks `matches` collection before recording. Unique compound index `(buy_id, sell_id)` as DB-level safety net.

---

### 6.7 Batch Processing Rate Limit Crash
**Problem:** `python main.py --text messages.txt` sent the entire file as one Gemini call. On large files, quota was exhausted mid-run with zero entries saved.

**Root cause:** The old `--text` handler read the whole file and passed it to `parse_text_message()` as one string. Also, free tier daily cap is 20 requests — a 183-message file uses 366+ calls minimum.

**Fix:** Per-message processing loop in `_process_text_file()`:
- File split by separator (auto-detected)
- Each message: parse → insert → sleep → next
- DB written after every message — crash or quota hit never loses already-processed entries
- Rate limit retry: on 429, waits `_RATE_LIMIT_WAIT` seconds and retries up to `_RATE_LIMIT_MAX_RETRIES` times
- Inter-message delay: `_MESSAGE_DELAY_SECONDS = 13` keeps requests under 5 RPM free tier limit

---

### 6.8 Sub-community Collapsing Into Parent Area
**Problem:** "Marina Gate" was resolving to "Dubai Marina" because "marina" appears in both and fuzzy matching pulled the shorter/more common one.

**Fix:** Token coverage scoring in Layer 2 of `location/resolver.py`. Input tokens `{marina, gate}` cover 100% of "Marina Gate" but only 50% of "Dubai Marina" — specific candidate wins. See Section 10.

---

### 6.9 Phase Number Lost on Concatenated Abbreviations
**Problem:** `ar3` was matching alias `ar` (Arabian Ranches) because the phase number wasn't being extracted before fuzzy matching.

**Fix:** `_split_phase()` in `location/resolver.py` handles both spaced (`arabian ranches 3`) and concatenated (`ar3`) forms before any comparison. Phase hard-block: if input has phase N and candidate has any different or no phase → candidate is dropped entirely, regardless of fuzzy score. See Section 10.

---

## 7. Config Reference (`core/config.py`)

| Constant | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | From `.env` |
| `MONGO_URI` | — | From `.env` |
| `API_KEY` | — | From `.env` — required on all API endpoints |
| `MONGO_DB_NAME` | `"realestate"` | Database name |
| `COLLECTION_BUY` | `"buy_listings"` | |
| `COLLECTION_SELL` | `"sell_listings"` | |
| `COLLECTION_MATCHES` | `"matches"` | |
| `PRICE_TOLERANCE` | `0.10` | ±10% price flexibility for matching |
| `DISTANCE_KM_TOLERANCE` | `2.0` | Max km between buyer and seller location |
| `BHK_TOLERANCE` | `0` | 0 = exact bedroom match; 1 = allow ±1 |
| `SQFT_TOLERANCE` | `0.15` | ±15% sqft tolerance; `None` = disable |
| `MIN_MATCH_SCORE` | `0.75` | Minimum weighted score to record a match |
| `REQUIRE_PRICE_OR_LOCATION` | `True` | Reject if both price and location are skipped |
| `FUZZY_LOCATION_THRESHOLD` | `85` | Legacy threshold — used in matcher, not resolver |
| `LOCATION_FUZZY_THRESHOLD` | `80` | Base fuzzy cutoff inside location/resolver Layer 2 |
| `DUPLICATE_DETECTION` | `True` | Enable fingerprint-based duplicate rejection |
| `DUPLICATE_PRICE_TOLERANCE` | `0.05` | **Defined but not yet wired into `_build_fingerprint()` — dead config** |
| `LOCATIONS_CSV` | `"data/locations.csv"` | Canonical names + aliases |
| `COORDINATES_CSV` | `"data/coordinates.csv"` | canonical_name → lat, lng |
| `USE_LEGACY_GEOCODING` | `False` | If False, only data/coordinates.csv is used for geocoding |
| `USE_NOMINATIM_GEOCODING` | `True` | Enables Nominatim fallback when legacy geocoding is on |
| `USE_GEMINI_PARSER_CLASSIFIER` | `False` | Toggle Gemini message classifier in `ingestion/parser.py` |
| `USE_GEMINI_PARSER_TEXT_EXTRACTION` | `True` | Toggle Gemini text extraction in `ingestion/parser.py` |
| `USE_GEMINI_PARSER_IMAGE_EXTRACTION` | `True` | Toggle Gemini image extraction in `ingestion/parser.py` |
| `USE_GEMINI_RESOLVER_DISAMBIGUATE` | `True` | Toggle Gemini disambiguation in `location/resolver.py` |
| `USE_GEMINI_RESOLVER_CONFIRM` | `True` | Toggle Gemini confirmation in `location/resolver.py` |
| `USE_GEMINI_RESOLVER_COLD_STEP_A` | `True` | Toggle Gemini cold Step A in `location/resolver.py` |
| `USE_GEMINI_RESOLVER_COLD_STEP_B` | `True` | Toggle Gemini cold Step B in `location/resolver.py` |
| `LOCATION_CSV_CANONICAL_COLUMN` | `"canonical_name"` | Column header in data/locations.csv |
| `LOCATION_CSV_ALIASES_COLUMN` | `"aliases"` | Column header in data/locations.csv |
| `LOCATION_RESOLUTION_CACHE_FILE` | `"cache/location_cache.json"` | Resolver output cache |
| `UNRESOLVED_LOG_FILE` | `"cache/unresolved_locations.log"` | Failed resolution log |
| `GEOCODE_CACHE_FILE` | `"cache/geocode_cache.json"` | Legacy Nominatim cache |
| `DELETE_AFTER_MATCH` | `False` | `True` = delete matched docs from buy/sell |
| `MODEL` | `"gemini-2.5-flash-lite"` | Gemini model — change here affects parser + resolver |

---

## 8. CLI Usage

```bash
# Parse a text file — messages separated by --- (auto-detected)
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

# Run as API server
uvicorn main:app --reload --port 8000

# Expose publicly (dev only — URL changes on restart)
ngrok http 8000
```

---

## 9. API Endpoints (`ingestion/api.py`)

All endpoints require `X-API-Key` header.

| Endpoint | Purpose |
|---|---|
| `POST /ingest/text` | Message string → filter → parse → store → match → return results |
| `POST /ingest/image` | Image upload → parse → store → match → return results |
| `GET /stats` | DB document counts |
| `GET /matches` | All matches, serialized |

**Response format:**
```json
{
  "filtered": false,
  "inserted": 2,
  "duplicates_skipped": 1,
  "listings": ["..."],
  "matches": [{
    "match_id": "...",
    "score": 0.85,
    "reasons": ["..."],
    "buy_broker": {},
    "sell_broker": {},
    "buy_snapshot": {},
    "sell_snapshot": {}
  }]
}
```

**Message filtering** (`is_real_estate_message()`): Hybrid rules + Gemini confidence gating. Rule score ≥ 4 → pass immediately (no Gemini call). Only drops message when Gemini says no with high confidence AND rule score is weak. Fails open on API errors — never drops on failure.

---

## 10. location/resolver.py — 4-Layer Pipeline

### On Startup
Reads `data/locations.csv`, builds:
- `_alias_map`: `normalized_key → canonical` (for O(1) exact lookup)
- `_alias_entries`: every alias pre-split into `{alias_norm, alias_base, alias_phase, canonical}`
- `_canonical_entries`: every canonical pre-split into `{canonical, base, phase}`
- `_canonical_list` and `_canonical_set` for Gemini prompts

All splitting done once at load time via `_split_phase()`. Not repeated at query time.

### Key Utilities

**`normalize_key(s)`:** lowercase → strip → remove dots → collapse whitespace.
`'D.S.O'` → `'dso'`, `'al  barsha'` → `'al barsha'`

**`_split_phase(s)`:** Extracts trailing number from a normalized string. Handles:
- Spaced: `'arabian ranches 3'` → `('arabian ranches', 3)`
- Concatenated: `'ar3'` → `('ar', 3)`
- No phase: `'business bay'` → `('business bay', None)`

### Layer 0 — Cache
Hit `cache/location_cache.json` first. Both successes and failures (`"UNKNOWN"`) are cached. Cache hit → return immediately, zero further processing.

### Layer 1 — Exact Alias Match
`normalize_key(input)` looked up directly in `_alias_map`. O(1) dict lookup. Catches known abbreviations (`jvc`, `dso`, `bbay`) and known typos already in the alias list.

### Layer 2 — Candidate Generation + Confidence Scoring

**Step 1 — Split input:**
`normalize_key(input)` → `_split_phase()` → `{input_base, input_phase}`

**Step 2 — Fuzzy match base only:**
`input_base` matched against every `alias_entry.alias_base` via `rapidfuzz.process.extract` with `WRatio`, cutoff 50, top 10. Cutoff is intentionally generous — confidence scoring does the real filtering.

**Step 3 — Score each candidate:**
```
confidence = 0.50 × fuzzy_norm
           + 0.35 × token_coverage
           + 0.15 × phase_score
```

*Fuzzy (0.50):* WRatio score / 100 on base strings only.

*Token coverage (0.35):* Fraction of input tokens found in candidate canonical name.
- `{marina, gate}` vs `Marina Gate` → 2/2 = 1.0
- `{marina, gate}` vs `Dubai Marina` → 1/2 = 0.5
- Single-token inputs → neutral 0.5 (avoids penalising abbreviations like `jvc`)

*Phase score (0.15):*
| Situation | Score |
|---|---|
| Input phase == candidate phase (or both None) | 1.0 |
| Input has phase N, candidate has phase N | 1.0 |
| Input has phase N, candidate has different or no phase | **-1.0 (hard block — dropped)** |
| Input has no phase, candidate has a phase | 0.3 (penalty, not block) |

**Step 4 — Deduplicate and sort:**
Keep best candidate per canonical, sort descending by confidence.

### Decision Gate

| Condition | Action |
|---|---|
| top ≥ 0.82 AND gap to 2nd ≥ 0.08 | Return directly, skip Gemini |
| top ≥ 0.82 BUT gap ≤ 0.08 | Ambiguous → Gemini disambiguate mode |
| top 0.65–0.82 | Medium confidence → Gemini confirm mode |
| top < 0.65 | Low confidence → Gemini cold mode |

Tunable thresholds at top of `location/resolver.py`:
```python
_CONFIDENCE_HIGH = 0.82
_CONFIDENCE_AMBIGUITY_BAND = 0.08
_CONFIDENCE_CONFIRM = 0.65
_W_FUZZY = 0.50
_W_TOKEN_COVERAGE = 0.35
_W_PHASE = 0.15
```

### Layer 3 — Gemini (3 Modes)

**Disambiguate:** Top-2 candidates are too close. Gemini sees original input + top candidates with scores and picks one.
```
Broker wrote: 'marina gate'
Could be:
  1. Marina Gate (confidence=0.83)
  2. Dubai Marina (confidence=0.81)
Which is correct?
```

**Confirm:** Medium confidence single candidate. Gemini validates or corrects with canonical list available.
```
Broker wrote: 'arabian ranch 3'
I think this is: 'Arabian Ranches 3' (confidence=0.71)
Is this correct?
```

**Cold:** Low confidence, no useful candidates. Original two-step behavior:
- Step A: open-ended — "what Dubai place is this?"
- If Step A answer not in canonical list → Step B constrained to canonical list
- If Step A returns UNKNOWN → skip Step B → unresolved

### Layer 4 — Unresolved Logger
All layers failed. Written to `cache/unresolved_locations.log`:
- Timestamp
- Raw input
- Top 3 candidates with full score breakdown
- What Gemini returned

Cached as `"UNKNOWN"` so next occurrence hits Layer 0 immediately.
Listing stored with `location_unresolved=True`, `location=None` — not discarded, still matched on price/type/BHK.

### How Key Problems Are Solved

**ar3 → Arabian Ranches 3 (not Arabian Ranches):**
```
split: base="ar", phase=3
fuzzy: "ar" matches alias base "ar" of Arabian Ranches (score=100) AND
       alias base "ar" of Arabian Ranches 3 (score=100)
phase: Arabian Ranches (phase=None) → input_phase=3, c_phase=None → HARD BLOCK
       Arabian Ranches 3 (phase=3) → match → score 1.0
→ only Arabian Ranches 3 survives
```

**marina gate → Marina Gate (not Dubai Marina):**
```
split: base="marina gate", phase=None, tokens={marina, gate}
fuzzy: "marina gate" vs "marina" (Dubai Marina alias) → ~78
       "marina gate" vs "marina gate" (if canonical) → 100
token coverage:
  Dubai Marina {dubai, marina}: overlap={marina} → 1/2 = 0.50
  Marina Gate {marina, gate}:   overlap={marina,gate} → 2/2 = 1.0
→ Marina Gate wins clearly
```

---

## 11. Batch Processing (`main.py`)

### `_split_messages(content)`
Auto-detects separator. Tries in order: `---`, `===`, `***`, `###`, `~~~`, double blank lines. First one that splits into >1 part wins.

### `_parse_with_retry(message, index, total)`
Wraps `parse_text_message()` with retry on 429/quota errors. Waits `_RATE_LIMIT_WAIT` seconds between attempts, up to `_RATE_LIMIT_MAX_RETRIES` attempts. Non-rate-limit errors skip immediately.

### `_process_text_file(filepath)`
Per-message loop:
```
for each message:
    parse → insert into DB → sleep(_MESSAGE_DELAY_SECONDS) → next
run_matching() once at the end
```
DB is written after every message — quota hit at message 50 of 183 still saves messages 1–49.

### Rate Limit Constants (top of `main.py`)
```python
_RATE_LIMIT_WAIT = 15          # seconds to wait on 429
_RATE_LIMIT_MAX_RETRIES = 3    # attempts per message before skipping
_MESSAGE_DELAY_SECONDS = 13    # delay between messages (~4.6 req/min, under 5 RPM free limit)
```
Tune `_MESSAGE_DELAY_SECONDS` down when on a paid Gemini tier.

---

## 12. make.com Integration

```
WhatsApp/Gmail message
  → make.com trigger
  → HTTP POST /ingest/text (X-API-Key header)
  → if matches in response → notify admin (Telegram/email)
  → admin approval system: deferred for later
```

**Local dev:** `uvicorn main:app --reload --port 8000` + `ngrok http 8000`
**Production:** Railway or Render (permanent URL, free tier available)

---

## 13. Known Issues / Open Items

| # | Issue | Status |
|---|---|---|
| 1 | `data/coordinates.csv` needs lat/lng filled in for all canonical areas | Open |
| 2 | `DUPLICATE_PRICE_TOLERANCE` defined in core/config.py, imported in core/database.py, never used in `_build_fingerprint()` — hardcodes 50000 instead | Open |
| 3 | Matching is 1-to-1 (best sell per buy). Top-3 multi-match not yet implemented | Open |
| 4 | Admin approval system for matches deferred — plan: make.com sends match to admin with Approve/Deny → `POST /matches/{id}/approve` | Open |
| 5 | Production deployment beyond ngrok — Railway or Render | Open |
| 6 | `data/locations.csv` needs phased communities as separate canonical rows (Arabian Ranches 2/3, JVC phases etc.) — resolver handles them without this but L1 exact match won't catch abbreviations like `ar3` | Open |
| 7 | Free tier daily cap is 20 req/day — insufficient for batch runs with 100+ messages. Paid Gemini API needed for production | Open |
| 8 | Changing `MODEL` in `core/config.py` only takes effect on process restart — in-memory Gemini instance is not hot-reloaded | Known behaviour |

---

## 14. Decision Log

| Decision | Alternatives Considered | Reason Chosen |
|---|---|---|
| Gemini for parsing | OpenAI GPT-4o, local LLMs | Already in use, multimodal (text + image), forced JSON, generous free tier |
| `data/coordinates.csv` over Nominatim | Nominatim, Google Maps API | Nominatim: 1 req/sec, fails on project names. CSV: instant, no API, we control data |
| 4-layer location resolver | Static alias map only, pure Gemini | Static maps can't cover Dubai's constantly growing project name space. Pure Gemini is slow and uses quota on every call. Layered approach: fast for common cases, smart for edge cases |
| Candidate generation + confidence scoring | Simple top-1 fuzzy match | Top-1 fuzzy ignores phase numbers and sub-community specificity. Scoring system handles both correctly by design |
| Token coverage in scoring | Pure fuzzy score | Fuzzy alone can't distinguish "Marina Gate" from "Dubai Marina" — both contain "marina". Token coverage gives specificity signal |
| Phase hard-block (not penalty) | Penalty score | A penalty still allows wrong-phase matches to win if fuzzy score is high enough. Hard block makes it impossible regardless of score |
| 3-mode targeted Gemini | Single open-ended Gemini call | Disambiguate/Confirm modes give Gemini better context → more accurate. Saves quota by skipping Gemini entirely on high-confidence direct matches |
| Per-message insert in batch | Parse all then insert all | All-at-once loses everything on quota hit mid-run. Per-message saves progress continuously |
| Auto separator detection | Fixed `---` separator | Users shouldn't need to know or conform to a specific format |
| `fuzz.WRatio` over `ratio` | `ratio`, `token_sort_ratio`, `partial_ratio` | WRatio handles concatenations (`businessbay`), token reordering, partial matches better |
| MD5 fingerprint for deduplication | Full-field exact match | Tolerates minor reprice reposts (±50K bucket) and sqft rounding |
| `MIN_MATCH_SCORE = 0.75` | 0.50, 0.60 | 0.50 and 0.60 allowed too many junk matches in testing |
| `response_mime_type: application/json` | Post-processing JSON extraction | Forces Gemini to output valid JSON directly — eliminates markdown fence stripping errors |
| Fail open on message filter | Fail closed | False negatives (dropping real listings) are worse than false positives (processing non-listings) |
| `location_unresolved=True` flag | Reject unresolved listings | Price/type/BHK data still valuable even when location is unknown |