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

import traceback as _traceback
import json
import re
import base64
from pathlib import Path
from core.gemini_client import call_gemini

from core.config import (
    USE_GEMINI_PARSER_CLASSIFIER,
    USE_GEMINI_PARSER_TEXT_EXTRACTION,
    USE_GEMINI_PARSER_IMAGE_EXTRACTION,
)

# ─────────────────────────────────────────────────────────────
# Gemini Setup
# ─────────────────────────────────────────────────────────────


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

The input may contain multiple listings with no clear separator.
You MUST return a JSON array, one object per listing.
Even if there is only one listing, return a JSON array.

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

    "location_raw": "<string or null>",
    "location_hint": {
        "city": "<string or null>",
        "community": "<string or null>",
        "subcommunity": "<string or null>",
        "property": "<string or null>"
    },

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

    "sent_by": "<string or null>",

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
    still capture the exact text in location_raw and keep location_hint fields null.

9. "Ready", "Vacant", "Ready to move"
   → is_ready=true

10. "handover 2027"
    → handover_year=2027

11. If not mentioned:
    use null.

12. amenities MUST always be a list.

13. broker MUST always exist.

14. raw_text should contain only the text relevant to THAT listing.

15. LOCATION FIELDS:
        location_raw = exact text as written in the message, or null if none.
        location_hint = best-effort hierarchy, or nulls if unsure.
        - Extract only what is mentioned or strongly implied in the message.
        - Do not infer hierarchy levels you are not confident about — use null.
        - If unsure whether a name is community or subcommunity, put it at subcommunity.
        - Do not split one location name across multiple levels unless certain.
        - City can often be inferred from well-known names (Dubai Marina -> Dubai,
            Yas Island -> Abu Dhabi) but use null if genuinely unclear.
        - Do not guess. A null is better than a wrong value.

16. Return ONLY JSON ARRAY.

17. sent_by:
        - Extract the original sender name if the message includes forwarding info
            like "Message sent by: Aarav" or "From Aarav:" or just name.
        - Use null when not present.
"""

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

VALIDATION_FIELDS = [
    "transaction",
    "property_type",
    "location_raw",
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

    # Transaction normalization
    t = str(listing.get("transaction", "sell")).lower().strip()

    if t not in ("buy", "sell"):
        t = "sell"

    listing["transaction"] = t

    # raw_loc = listing.get("location")
    # if raw_loc:
    #     resolved = resolve_location(raw_loc)
    #     listing["location"] = resolved if resolved else raw_loc

    raw_loc = listing.get("location_raw")
    if not raw_loc:
        raw_loc = listing.get("location")
    listing["location_raw"] = raw_loc if raw_loc else None
    listing.pop("location", None)

    hint = listing.get("location_hint")
    if not isinstance(hint, dict):
        hint = {}

    def _hint_value(key: str) -> str | None:
        value = hint.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    listing["location_hint"] = {
        "city": _hint_value("city"),
        "community": _hint_value("community"),
        "subcommunity": _hint_value("subcommunity"),
        "property": _hint_value("property"),
    }

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

    sent_by = listing.get("sent_by")
    if sent_by is None:
        listing["sent_by"] = None
    else:
        sent_by = str(sent_by).strip()
        listing["sent_by"] = sent_by or None

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
    if not USE_GEMINI_PARSER_CLASSIFIER:
        return None, None

    prompt = (
        "You are a classifier for a Dubai real estate WhatsApp group.\n"
        "Is the following message a real estate listing, property requirement, or buying/selling inquiry?\n"
        "Reply ONLY with valid JSON in this exact format:\n"
        '{"is_real_estate": true|false, "confidence": 0.0-1.0}\n\n'
        f"Message: {text}"
    )

    response = call_gemini(prompt, generation_config={"temperature": 0.1, "response_mime_type": "application/json"})
    if response is None:
        return None, None
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
    if not USE_GEMINI_PARSER_CLASSIFIER:
        return True

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

def old_parse_text_message(message: str) -> list[dict]:
    """
    Parse WhatsApp text message into structured listings.
    """

    if not USE_GEMINI_PARSER_TEXT_EXTRACTION:
        print("[Parser] Gemini extraction disabled; skipping text parse.")
        return []

    try:

        response = call_gemini(
            [SYSTEM_PROMPT, f"\nMESSAGE:\n{message}"],
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
        )
        if response is None:
            print("[Parser] Invalid JSON returned")
            return []

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

# Buy-signal keywords — if Gemini returns "sell" but message contains these, flip to buy
_BUY_SIGNALS = [
    "looking for", "requirement", "wanted", "budget", "pre-approved",
    "investor looking", "ready to close", "cash buyer", "mortgage buyer",
    "client looking", "client need", "client require", "need apartment",
    "need villa", "need property", "seeking", "require",
]

def parse_text_message(message: str) -> list[dict]:
    """
    Parse WhatsApp text message into structured listings.
    Includes post-parse heuristic to catch Gemini transaction misclassification.
    """
    if not USE_GEMINI_PARSER_TEXT_EXTRACTION:
        print("[Parser] Gemini extraction disabled; skipping text parse.")
        return []

    raw_gemini_output = ""
    try:
        response = call_gemini(
            [SYSTEM_PROMPT, f"\nMESSAGE:\n{message}"],
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
        )
        if response is None:
            _log_parse_failure(message, "", "Gemini returned None")
            return []

        raw_gemini_output = response.text or ""
        raw = _clean_json(raw_gemini_output)
        data = _safe_json_loads(raw)

        if data is None:
            _log_parse_failure(message, raw_gemini_output, "Invalid JSON returned")
            return []

        if not isinstance(data, list):
            data = [data]

        listings = _validate_listings(data)

        # ── Post-parse heuristic: catch transaction misclassification ──────────
        # Gemini sometimes returns "sell" for clear buy requirements.
        # If the raw message contains strong buy signals, flip transaction to "buy".
        message_lower = message.lower()
        for listing in listings:
            if listing.get("transaction") == "sell":
                found_signals = [s for s in _BUY_SIGNALS if s in message_lower]
                if found_signals:
                    listing["transaction"] = "buy"
                    try:
                        from core.logger import log_event
                        log_event(
                            "parse_warning", "warning", "parser",
                            f"Transaction flipped sell→buy based on keyword signals in message",
                            {
                                "signals_found":   found_signals,
                                "raw_snippet":     message[:300],
                                "original_output": raw_gemini_output[:300],
                                "location_raw":    listing.get("location_raw"),
                                "property_type":   listing.get("property_type"),
                            },
                        )
                    except Exception:
                        pass
                    print(f"[Parser] ⚠ Transaction flipped sell→buy — signals: {found_signals}")

        return listings

    except Exception as e:
        _log_parse_failure(message, raw_gemini_output, str(e), exc=e)
        return []


def _log_parse_failure(message: str, raw_output: str, reason: str, exc: Exception | None = None) -> None:
    """Log a parse failure to terminal and MongoDB."""
    print(f"[Parser] ✗ Parse failed: {reason}")
    if raw_output:
        print(f"[Parser]   Raw output snippet: {raw_output[:200]}")
    try:
        from core.logger import log_event
        log_event(
            "parse_failure", "error", "parser",
            f"Parse failed: {reason}",
            {
                "reason":            reason,
                "raw_message_snippet": message[:300],
                "raw_gemini_output":   raw_output[:500],
                "traceback":           _traceback.format_exc() if exc else "",
            },
        )
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Image Parsing
# ─────────────────────────────────────────────────────────────

def parse_image(
    image_path: str | None = None,
    *,
    data_b64: str | None = None,
    mime_type: str = "image/jpeg",
    context_text: str | None = None,
) -> list[dict]:
    """
    Parse flyer/image into listings.

    Supports either a filesystem path (backwards compatible) or a base64-encoded
    image passed via `data_b64`. The `mime_type` is forwarded to the Gemini
    client and defaults to "image/jpeg". When provided, `context_text` is added
    to the prompt for extra message context.
    """

    if not USE_GEMINI_PARSER_IMAGE_EXTRACTION:
        print("[Parser] Gemini extraction disabled; skipping image parse.")
        return []

    encoded = None

    # If base64 data provided, prefer that (used by multipart endpoints)
    if data_b64:
        encoded = data_b64

    # Otherwise, fall back to reading from a filesystem path (existing behavior)
    elif image_path:
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

        # If caller didn't explicitly set mime_type, infer from suffix
        if mime_type == "image/jpeg":
            mime_type = mime_types.get(path.suffix.lower(), mime_type)

        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

    else:
        print("[Parser] No image data provided to parse_image")
        return []

    image_part = {
        "mime_type": mime_type,
        "data": encoded,
    }

    try:

        prompt_parts = [SYSTEM_PROMPT, "Extract all listings from this image."]
        if context_text:
            context_text = context_text.strip()
            if context_text:
                prompt_parts.append(f"Additional context message:\n{context_text}")

        response = call_gemini(
            prompt_parts,
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
            image_part=image_part,
        )
        if response is None:
            _log_parse_failure("", "", "Gemini returned None for image")
            return []

        raw = _clean_json(response.text)
        data = _safe_json_loads(raw)

        if data is None:
            _log_parse_failure("", response.text or "", "Invalid JSON from image parse")
            return []

        if not isinstance(data, list):
            data = [data]

        return _validate_listings(data)

    except Exception as e:
        _log_parse_failure("", "", f"Image parse exception: {e}", exc=e)
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