"""
parser.py

Uses Gemini API to extract structured Dubai real estate listings
from WhatsApp text messages and flyer images.

Features:
- Multiple listings per message
- Buy vs Sell detection
- Broker/contact extraction
- JSON cleaning + validation
- Strong anti-cross-contamination rules
- Works with text + images
"""

import json
import re
import base64
from pathlib import Path

import google.generativeai as genai

from config import GEMINI_API_KEY, MODEL

# ─────────────────────────────────────────────────────────────
# Gemini Setup
# ─────────────────────────────────────────────────────────────

genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel(
    model_name=MODEL,
    generation_config={
        "temperature": 0.1,
        "response_mime_type": "application/json",
    },
)

# ─────────────────────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an expert Dubai real estate listing extraction engine.

Your task:
Extract ALL property listings independently from WhatsApp messages or images.

Return ONLY valid JSON.
NO markdown.
NO explanation.
NO comments.

Return a JSON ARRAY.

Each listing MUST follow this exact schema:

{
  "transaction": "buy" | "sell",

  "property_type":
    "apartment" |
    "villa" |
    "townhouse" |
    "plot" |
    "warehouse" |
    "duplex" |
    "penthouse" |
    "studio" |
    "office" |
    "other",

  "location": "<string or null>",

  "price_aed": <number or null>,
  "price_min_aed": <number or null>,
  "price_max_aed": <number or null>,

  "price_per_sqft_aed": <number or null>,

  "bhk": <integer or null>,

  "sqft": <number or null>,
  "plot_sqft": <number or null>,

  "is_ready": <true | false | null>,
  "handover_year": <integer or null>,

  "payment_plan": "<string or null>",

  "furnishing":
    "furnished" |
    "semi-furnished" |
    "unfurnished" |
    null,

  "amenities": ["list"],

  "is_distress": <true | false>,
  "is_mortgage": <true | false>,
  "is_cash": <true | false>,

  "broker": {
    "name": "<string or null>",
    "phone": "<string or null>",
    "company": "<string or null>"
  },

  "notes": "<string or null>",

  "raw_text": "<only the relevant snippet for THIS listing>"
}

IMPORTANT EXTRACTION RULES:

1. ONE MESSAGE CAN CONTAIN MULTIPLE LISTINGS.
   Each listing MUST be independent.

2. NEVER copy values from one listing into another.

3. BUY DETECTION:
   Use "buy" if message says:
   - client requirement
   - looking for
   - wanted
   - requirement
   - pre-approved buyer
   - investor looking
   - budget
   - ready to close
   - cash buyer
   - mortgage buyer

4. Otherwise default to "sell".

5. PRICE NORMALIZATION:
   - 2.5M → 2500000
   - 850K → 850000
   - 2.4 Million → 2400000

6. BUYER BUDGET RANGE:
   "4-5M" →
   price_min_aed=4000000
   price_max_aed=5000000
   price_aed=4500000

7. SINGLE BUYER BUDGET:
   "max 2.9M"
   → price_aed=2900000

8. If location unclear:
   use the project/community name anyway.

9. "Ready", "Vacant", "Ready to move"
   → is_ready=true

10. "handover 2027"
    → handover_year=2027

11. If not mentioned:
    use null.

12. amenities MUST always be a list.

13. broker MUST always exist.

14. raw_text should contain only the text relevant to THAT listing.

15. LOCATION EXTRACTION:
    Extract the location string EXACTLY as written in the message.
    Do NOT interpret, expand, or correct it.
    "jvc" → "jvc"
    "dso" → "dso"
    "bbay" → "bbay"
    "d.i.f.c" → "d.i.f.c"
    "al  barsha" → "al  barsha"
    Abbreviations, typos, and slang are VALID — extract as-is.
    Resolution happens downstream. Your job is extraction only.

16. Return ONLY JSON ARRAY.
"""

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

VALIDATION_FIELDS = [
    "transaction",
    "property_type",
    "location",
    "price_aed",
]


def _clean_json(text: str) -> str:
    """
    Cleans Gemini output.
    """
    text = text.strip()

    text = re.sub(r"^```json", "", text)
    text = re.sub(r"^```", "", text)
    text = re.sub(r"```$", "", text)

    return text.strip()


def _safe_json_loads(text: str):
    """
    Safe JSON parsing.
    """
    try:
        return json.loads(text)
    except Exception:
        return None


def _normalize_listing(listing: dict) -> dict:
    """
    Ensures required structure/types exist.
    """

    from location_resolver import resolve_location

    # Transaction normalization
    t = str(listing.get("transaction", "sell")).lower().strip()

    if t not in ("buy", "sell"):
        t = "sell"

    listing["transaction"] = t

    # raw_loc = listing.get("location")
    # if raw_loc:
    #     resolved = resolve_location(raw_loc)
    #     listing["location"] = resolved if resolved else raw_loc

    raw_loc = listing.get("location")
    if raw_loc:
        resolved = resolve_location(raw_loc)
        if resolved:
            listing["location"] = resolved
            listing["location_unresolved"] = False
        else:
            listing["location"] = None
            listing["location_unresolved"] = True
            listing["location_raw"] = raw_loc
    else:
        listing["location_unresolved"] = False

    # Amenities always list
    if not isinstance(listing.get("amenities"), list):
        listing["amenities"] = []

    # Broker always dict
    broker = listing.get("broker")

    if not isinstance(broker, dict):
        broker = {}

    listing["broker"] = {
        "name": broker.get("name"),
        "phone": broker.get("phone"),
        "company": broker.get("company"),
    }

    # Boolean defaults
    for field in ["is_distress", "is_mortgage", "is_cash"]:
        if listing.get(field) is None:
            listing[field] = False

    return listing


def _validate_listings(listings: list[dict]) -> list[dict]:
    """
    Removes junk listings.
    """

    valid = []

    for i, listing in enumerate(listings):

        if not isinstance(listing, dict):
            continue

        has_data = any(listing.get(f) for f in VALIDATION_FIELDS)

        if not has_data:
            print(f"[Parser] Dropped empty listing #{i+1}")
            continue

        valid.append(_normalize_listing(listing))

    return valid


# ─────────────────────────────────────────────────────────────
# Text Parsing
# ─────────────────────────────────────────────────────────────

def _rule_score(text: str) -> int:
    t = text.lower()
    score = 0

    if re.search(r"\bfor\s+sale\b|\bfor\s+rent\b|\bto\s+rent\b|\blease\b", t):
        score += 2

    if re.search(r"\b(looking\s+for|requirement|wanted|budget|cash\s+buyer|mortgage\s+buyer)\b", t):
        score += 2

    if re.search(r"\b\d+\s*(br|bhk)\b", t):
        score += 2

    if re.search(r"\b(aed|dhs|dirham)\b", t) and re.search(r"\d", t):
        score += 2

    if re.search(r"\b\d+(\.\d+)?\s*(m|million|k)\b", t):
        score += 2

    if "sqft" in t or "sq ft" in t:
        score += 1

    if re.search(r"\b(studio|apartment|villa|townhouse|penthouse|duplex|plot|office|warehouse)\b", t):
        score += 1

    return score


def _classify_with_gemini(text: str) -> tuple[bool | None, float | None]:
    prompt = (
        "You are a classifier for a Dubai real estate WhatsApp group.\n"
        "Is the following message a real estate listing, property requirement, or buying/selling inquiry?\n"
        "Reply ONLY with valid JSON in this exact format:\n"
        '{"is_real_estate": true|false, "confidence": 0.0-1.0}\n\n'
        f"Message: {text}"
    )

    response = model.generate_content(prompt)
    data = _safe_json_loads(response.text.strip())
    if isinstance(data, dict):
        label = data.get("is_real_estate")
        confidence = data.get("confidence")
        if isinstance(label, bool) and isinstance(confidence, (int, float)):
            return label, float(confidence)

    cleaned = response.text.strip().lower()
    if cleaned in ("yes", "no"):
        return cleaned == "yes", 0.5

    return None, None

def is_real_estate_message(text: str) -> bool:
    """
    Returns True if the message is a real estate listing or requirement.
    Fail open on API errors to avoid dropping messages.
    """
    try:
        score = _rule_score(text)
        if score >= 4:
            return True

        label, confidence = _classify_with_gemini(text)

        if label is None:
            return True

        if label is False and confidence is not None and confidence >= 0.8 and score <= 1:
            return False

        if label is True and confidence is not None and confidence >= 0.6:
            return True

        return score >= 2
    except Exception as e:
        print(f"[Parser] Real estate filter failed: {e}")
        return True

def parse_text_message(message: str) -> list[dict]:
    """
    Parse WhatsApp text message into structured listings.
    """

    try:

        response = model.generate_content(
            [
                SYSTEM_PROMPT,
                f"\nMESSAGE:\n{message}",
            ]
        )

        raw = _clean_json(response.text)

        data = _safe_json_loads(raw)

        if data is None:
            print("[Parser] Invalid JSON returned")
            print(raw[:500])
            return []

        if not isinstance(data, list):
            data = [data]

        return _validate_listings(data)

    except Exception as e:
        print(f"[Parser] Error parsing text: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# Image Parsing
# ─────────────────────────────────────────────────────────────

def parse_image(image_path: str) -> list[dict]:
    """
    Parse flyer/image into listings.
    """

    path = Path(image_path)

    if not path.exists():
        print(f"[Parser] Image not found: {image_path}")
        return []

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    mime_type = mime_types.get(path.suffix.lower(), "image/jpeg")

    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    image_part = {
        "mime_type": mime_type,
        "data": encoded,
    }

    try:

        response = model.generate_content(
            [
                SYSTEM_PROMPT,
                {
                    "inline_data": image_part
                },
                "Extract all listings from this image.",
            ]
        )

        raw = _clean_json(response.text)

        data = _safe_json_loads(raw)

        if data is None:
            print("[Parser] Invalid JSON returned from image")
            return []

        if not isinstance(data, list):
            data = [data]

        return _validate_listings(data)

    except Exception as e:
        print(f"[Parser] Error parsing image: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# Auto Input Detection
# ─────────────────────────────────────────────────────────────

def parse_input(source: str) -> list[dict]:
    """
    Auto detect image vs text.
    """

    image_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
    }

    p = Path(source)

    if p.exists() and p.suffix.lower() in image_extensions:
        return parse_image(source)

    return parse_text_message(source)