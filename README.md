# Real Estate WhatsApp Message Matcher

Parses Dubai real estate WhatsApp messages (text + images) → MongoDB → auto-matches buy/sell listings.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Set your API keys in `.env`:
```dotenv
GEMINI_API_KEY=your_gemini_api_key_here
MONGO_URI=your_mongodb_connection_string_here
API_KEY=your_api_key_here
```

Run the API server:
```bash
uvicorn main:app --reload --port 8000
```

## Tuning (all in config.py — one line each)

| Constant | Default | Meaning |
|---|---|---|
| `PRICE_TOLERANCE` | `0.10` | ±10% price flexibility |
| `DISTANCE_KM_TOLERANCE` | `2.0` | ±2 km location radius |
| `BHK_TOLERANCE` | `0` | 0 = exact BHK, 1 = allow ±1 bedroom |
| `SQFT_TOLERANCE` | `0.15` | ±15% sqft, set `None` to disable |
| `DELETE_AFTER_MATCH` | `False` | `True` = delete matched listings from DB |

## Usage

```bash
# Parse a .txt file of WhatsApp messages + run matching
python main.py --text messages.txt

# Parse an image flyer + run matching
python main.py --image flyer.png

# Paste raw message directly
python main.py --raw "FOR SALE | Business Bay | 2BR | AED 2.5M"

# Run matching on existing DB only
python main.py --match-only

# Database stats
python main.py --stats

# View all matches
python main.py --show-matches

# ⚠️ Clear everything (testing)
python main.py --clear
```

## API Endpoints

All endpoints require `X-API-Key` header.

### POST /ingest/text

Body:
```json
{ "message": "raw whatsapp message string" }
```

Response (filtered):
```json
{ "filtered": true, "reason": "not a real estate message" }
```

Response (processed):
```json
{
  "filtered": false,
  "inserted": 2,
  "duplicates_skipped": 1,
  "listings": [ ... ],
  "matches": [ ... ]
}
```

### POST /ingest/image

Multipart upload with `file` field.

### GET /stats

Returns counts for buy/sell/matches.

### GET /matches

Returns all matches.

## MongoDB Collections

| Collection | Contents |
|---|---|
| `buy_listings` | Parsed buyer requirements |
| `sell_listings` | Parsed property listings for sale |
| `matches` | Matched pairs with score + reasons |

### Listing Document Schema

```json
{
  "transaction": "buy" | "sell",
  "property_type": "apartment" | "villa" | "townhouse" | "plot" | ...,
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
  "payment_plan": "60% paid, 30% during construction, 10% on handover",
  "furnishing": null,
  "amenities": ["canal view", "parking"],
  "is_distress": false,
  "is_mortgage": false,
  "is_cash": false,
  "notes": "Brand new, vacant",
  "raw_text": "...",
  "created_at": "2025-01-01T00:00:00Z",
  "matched": false,
  "match_id": null
}
```

### Match Document Schema

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
  "buy_snapshot": { ... },
  "sell_snapshot": { ... },
  "matched_at": "2025-01-01T00:00:00Z"
}
```

## File Structure

```
RealEstate/
├── config.py          ← All tunable constants
├── parser.py          ← Gemini parsing (text + images)
├── geocoder.py        ← Gemini normalization + Nominatim + Haversine
├── database.py        ← MongoDB CRUD
├── matcher.py         ← Buy/sell matching engine
├── api.py             ← FastAPI server
├── main.py            ← CLI entry point + FastAPI app
├── requirements.txt
└── geocode_cache.json ← Auto-created, caches geocoded locations
```