# Real Estate WhatsApp Message Matcher

Parses Dubai real estate WhatsApp messages (text + images) → MongoDB → auto-matches buy/sell listings.

## Setup

```bash
pip install -r requirements.txt
```

Set your API keys in `config.py`:
```python
GEMINI_API_KEY = "..."        # console.cloud.google.com → Gemini API
GOOGLE_MAPS_API_KEY = "..."   # console.cloud.google.com → Maps Geocoding API
MONGO_URI = "mongodb://localhost:27017"  # or Atlas URI
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
realestate_matcher/
├── config.py          ← All tunable constants
├── parser.py          ← Gemini parsing (text + images)
├── geocoder.py        ← Google Maps geocoding + Haversine
├── database.py        ← MongoDB CRUD
├── matcher.py         ← Buy/sell matching engine
├── main.py            ← CLI entry point
├── requirements.txt
└── geocode_cache.json ← Auto-created, caches geocoded locations
```