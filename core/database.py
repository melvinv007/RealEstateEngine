"""
database.py
MongoDB operations with:
- Duplicate fingerprint detection
- Historical match prevention
- Index creation for fast pre-filtering
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import MongoClient, ASCENDING
from rapidfuzz import fuzz

from core.config import (
    MONGO_URI, MONGO_DB_NAME,
    COLLECTION_BUY, COLLECTION_SELL, COLLECTION_MATCHES,
    COLLECTION_PROJECTS, COLLECTION_PROJECT_MATCHES,
    DUPLICATE_DETECTION_BUY, DUPLICATE_DETECTION_SELL,
    DUPLICATE_PRICE_TOLERANCE,
    DUPLICATE_SIZE_TOLERANCE,
    DUPLICATE_RAW_TEXT_FUZZY_THRESHOLD,
    DUPLICATE_MIN_FIELD_MATCHES,
    DUPLICATE_CANDIDATE_LIMIT,
)
from core.logger import COLLECTION_LOGS

_client = None
_db = None


def get_db():
    global _client, _db
    if _db is None:
        _client = MongoClient(MONGO_URI)
        _db = _client[MONGO_DB_NAME]
        _ensure_indexes(_db)
    return _db


def _ensure_indexes(db):
    """
    Create indexes for fast MongoDB pre-filtering.
    Called once on first DB connection.
    These turn O(n×m) full scans into indexed lookups.
    """
    for coll_name in (COLLECTION_BUY, COLLECTION_SELL):
        coll = db[coll_name]
        coll.create_index([("matched", ASCENDING)])
        coll.create_index([("property_type", ASCENDING), ("matched", ASCENDING)])
        coll.create_index([("bhk", ASCENDING), ("matched", ASCENDING)])
        coll.create_index([("price_aed", ASCENDING), ("matched", ASCENDING)])
        coll.create_index([("fingerprint", ASCENDING)], unique=False)
        coll.create_index([("source_message_hash", ASCENDING), ("source_listing_index", ASCENDING)])
        coll.create_index([("raw_text_hash", ASCENDING)])
        coll.create_index([("wa_message_id", ASCENDING)])
        coll.create_index([("location", ASCENDING)])
        coll.create_index([("location_coords", "2dsphere")])

    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("sell_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING), ("sell_id", ASCENDING)], unique=True)

    projects = db[COLLECTION_PROJECTS]
    projects.create_index([("location_coords", "2dsphere")])
    projects.create_index([("project_fingerprint", ASCENDING)], unique=True)

    project_matches = db[COLLECTION_PROJECT_MATCHES]
    project_matches.create_index([("buy_id", ASCENDING), ("project_id", ASCENDING)], unique=True)

    # system_logs TTL + query indexes — created via logger module
    from core.logger import _ensure_log_indexes
    _ensure_log_indexes(db[COLLECTION_LOGS])


def _collection(name: str):
    return get_db()[name]


def _has_geojson_point(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") != "Point":
        return False
    coords = value.get("coordinates")
    return isinstance(coords, (list, tuple)) and len(coords) == 2


def _apply_location_metadata(listing: dict) -> None:
    resolver = None
    if isinstance(listing.get("location_resolution"), dict):
        resolver = listing.get("location_resolution")
    elif isinstance(listing.get("location_result"), dict):
        resolver = listing.get("location_result")

    if resolver:
        listing["location"] = resolver.get("matched_canonical")
        listing["location_level"] = resolver.get("matched_level")
        listing["location_city"] = resolver.get("city")
        listing["location_community"] = resolver.get("community")
        listing["location_subcommunity"] = resolver.get("subcommunity")
        listing["location_property"] = resolver.get("property")
        listing["location_confidence"] = resolver.get("confidence")
        listing["location_resolution_path"] = resolver.get("resolution_path")
        listing["location_unresolved"] = resolver.get("location_unresolved")

    coords_payload = listing.get("location_coords") if _has_geojson_point(listing.get("location_coords")) else None

    if coords_payload is None and resolver:
        coords = resolver.get("coords")
        if coords and coords.get("lat") is not None and coords.get("lng") is not None:
            coords_payload = {
                "type": "Point",
                "coordinates": [coords["lng"], coords["lat"]],
            }
            listing["location_coords"] = coords_payload


# ── Fingerprint / Duplicate Detection OLD ─────────────────────────────────────────

def old_build_fingerprint(listing: dict) -> str:
    """
    Build a hash from the listing's key identity fields.
    Two listings with the same fingerprint are considered duplicates.
    Price is rounded to nearest DUPLICATE_PRICE_TOLERANCE to handle minor reposts.
    """
    price = listing.get("price_aed") or 0
    # Round price to nearest DUPLICATE_PRICE_TOLERANCE to tolerate trivial reprice changes
    # price_bucket = round(price / DUPLICATE_PRICE_TOLERANCE) * DUPLICATE_PRICE_TOLERANCE
    price_bucket = round(price / 50000) * 50000

    identity = {
        "type": (listing.get("property_type") or "").lower().strip(),
        "location": (listing.get("location") or "").lower().strip(),
        "bhk": listing.get("bhk"),
        "price_bucket": price_bucket,
        "sqft": round((listing.get("sqft") or 0) / 100) * 100,  # round to nearest 100 sqft
        "transaction": listing.get("transaction", "sell"),
    }
    raw = json.dumps(identity, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def old_is_duplicate(listing: dict, coll_name: str, enabled: bool) -> dict | None:
    """Return the existing duplicate document if found; otherwise None."""
    if not enabled:
        return None

    fp = _build_fingerprint(listing)
    exists = _collection(coll_name).find_one({"fingerprint": fp})
    if exists:
        msg = f"[DB] Duplicate: {listing.get('location')} {listing.get('bhk')}BR {listing.get('price_aed')}"
        print(msg)
        try:
            from core.logger import log_event
            log_event("duplicate_detected", "info", "database", msg, {
                "fingerprint":   fp,
                "existing_id":   str(exists.get("_id")),
                "transaction":   listing.get("transaction"),
                "location":      listing.get("location"),
                "price_aed":     listing.get("price_aed"),
                "bhk":           listing.get("bhk"),
                "property_type": listing.get("property_type"),
            })
        except Exception:
            pass
        return exists
    return None

# ── Fingerprint / Duplicate Detection ─────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _value_present(value: object) -> bool:
    """True only for meaningful non-null values. False is meaningful for booleans."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_value_present(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    return True


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalise_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _hash_text(value: object) -> str | None:
    text = _normalise_text(value)
    if not text:
        return None
    return hashlib.md5(text.encode()).hexdigest()


def _normalise_phone(value: object) -> str:
    if value is None:
        return ""
    digits = re.sub(r"\D+", "", str(value))
    # +971501234567 and 0501234567 both become 501234567
    return digits[-9:] if len(digits) >= 9 else digits


def _numbers_within_percent(a: object, b: object, tolerance: float) -> bool:
    av = _to_float(a)
    bv = _to_float(b)
    if av is None or bv is None:
        return False
    if av <= 0 or bv <= 0:
        return False
    diff_ratio = abs(av - bv) / max(abs(av), abs(bv), 1.0)
    return diff_ratio <= tolerance


def _percentage_price_bucket(price: object) -> int | None:
    """
    Fingerprint helper only.

    Main duplicate decision uses true percentage comparison.
    This bucket just keeps old fingerprint-style dedupe useful.
    """
    value = _to_float(price)
    if value is None or value <= 0:
        return None

    # At 5%, this is 50,000 per 1M.
    # So changing DUPLICATE_PRICE_TOLERANCE actually changes fingerprint bucket size.
    bucket_size = max(1, int(1_000_000 * DUPLICATE_PRICE_TOLERANCE))
    return int(round(value / bucket_size) * bucket_size)


def _build_fingerprint(listing: dict) -> str:
    """
    Build a rough hash from key identity fields.

    Important:
    This is no longer the only duplicate detector.
    Fuzzy duplicate detection below is the main system.
    """
    identity = {
        "type": (listing.get("property_type") or "").lower().strip(),
        "location": (listing.get("location") or "").lower().strip(),
        "bhk": listing.get("bhk"),
        "price_bucket": _percentage_price_bucket(listing.get("price_aed")),
        "sqft": round((listing.get("sqft") or 0) / 100) * 100,
        "plot_sqft": round((listing.get("plot_sqft") or 0) / 100) * 100,
        "transaction": (listing.get("transaction") or "sell").lower().strip(),
    }
    raw = json.dumps(identity, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _prepare_duplicate_metadata(listing: dict) -> None:
    """
    Adds hash fields used for duplicate detection.

    full_raw_message is temporary input from API.
    We store only hash/snippet to avoid bloating buy/sell collections.
    """
    source_text = listing.get("full_raw_message") or listing.get("source_message")
    if source_text:
        listing["source_message_hash"] = _hash_text(source_text)
        listing["source_message_snippet"] = str(source_text)[:500]

    raw_text = listing.get("raw_text")
    if raw_text:
        listing["raw_text_hash"] = _hash_text(raw_text)

    listing.pop("full_raw_message", None)
    listing.pop("source_message", None)


def _field_text_match(a: object, b: object, threshold: int = 94) -> bool:
    ta = _normalise_text(a)
    tb = _normalise_text(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return fuzz.WRatio(ta, tb) >= threshold


def _broker_phone(doc: dict) -> str:
    broker = doc.get("broker") or {}
    if not isinstance(broker, dict):
        return ""
    return _normalise_phone(broker.get("phone"))


def _score_duplicate_candidate(new: dict, existing: dict) -> dict:
    """
    Score duplicate candidate using non-null field matches.

    Duplicate rule:
    - at least DUPLICATE_MIN_FIELD_MATCHES matched fields
    - at least one important field matched:
      location OR price OR sqft OR plot_sqft
    """
    matched_fields: list[str] = []
    important_fields: list[str] = []
    details: dict = {}

    # property type
    new_type = _normalise_text(new.get("property_type"))
    old_type = _normalise_text(existing.get("property_type"))
    if new_type and old_type and new_type == old_type and new_type != "other":
        matched_fields.append("property_type")

    # location
    new_loc = new.get("location") or new.get("location_raw")
    old_loc = existing.get("location") or existing.get("location_raw")
    if _field_text_match(new_loc, old_loc, threshold=92):
        matched_fields.append("location")
        important_fields.append("location")

    # BHK
    if new.get("bhk") is not None and existing.get("bhk") is not None:
        if new.get("bhk") == existing.get("bhk"):
            matched_fields.append("bhk")

    # price
    if _numbers_within_percent(
        new.get("price_aed"),
        existing.get("price_aed"),
        DUPLICATE_PRICE_TOLERANCE,
    ):
        matched_fields.append("price_aed")
        important_fields.append("price_aed")

    # sqft
    if _numbers_within_percent(
        new.get("sqft"),
        existing.get("sqft"),
        DUPLICATE_SIZE_TOLERANCE,
    ):
        matched_fields.append("sqft")
        important_fields.append("sqft")

    # plot sqft
    if _numbers_within_percent(
        new.get("plot_sqft"),
        existing.get("plot_sqft"),
        DUPLICATE_SIZE_TOLERANCE,
    ):
        matched_fields.append("plot_sqft")
        important_fields.append("plot_sqft")

    # broker phone
    new_phone = _broker_phone(new)
    old_phone = _broker_phone(existing)
    if new_phone and old_phone and new_phone == old_phone:
        matched_fields.append("broker_phone")

    # per-listing raw text fuzzy match
    new_raw = new.get("raw_text")
    old_raw = existing.get("raw_text")
    raw_similarity = None
    if _value_present(new_raw) and _value_present(old_raw):
        raw_similarity = fuzz.token_set_ratio(
            _normalise_text(new_raw),
            _normalise_text(old_raw),
        )
        details["raw_text_similarity"] = raw_similarity
        if raw_similarity >= DUPLICATE_RAW_TEXT_FUZZY_THRESHOLD:
            matched_fields.append("raw_text_fuzzy")

    duplicate = (
        len(matched_fields) >= DUPLICATE_MIN_FIELD_MATCHES
        and len(important_fields) >= 1
    )

    # Score is only for choosing the best duplicate candidate.
    score = len(matched_fields)
    if raw_similarity is not None:
        score += raw_similarity / 100.0

    return {
        "is_duplicate": duplicate,
        "score": round(score, 4),
        "matched_fields": matched_fields,
        "important_fields": important_fields,
        "details": details,
        "reason": (
            f"{len(matched_fields)} field(s) matched; "
            f"important={important_fields or []}"
        ),
    }


def _find_exact_raw_duplicate(listing: dict, coll) -> tuple[dict | None, dict | None]:
    """
    Exact same source message should short-circuit duplicate detection.

    For multi-listing WhatsApp messages, source_listing_index prevents all listings
    from mapping to the first old listing.
    """
    source_hash = listing.get("source_message_hash")
    source_index = listing.get("source_listing_index")

    if source_hash:
        query = {"source_message_hash": source_hash}
        if source_index is not None:
            query["source_listing_index"] = source_index

        existing = coll.find_one(query)
        if existing:
            return existing, {
                "is_duplicate": True,
                "score": 999.0,
                "matched_fields": ["source_message_hash", "source_listing_index"],
                "important_fields": ["source_message_hash"],
                "reason": "Exact same raw source message received again",
                "details": {
                    "source_message_hash": source_hash,
                    "source_listing_index": source_index,
                },
            }

    raw_hash = listing.get("raw_text_hash")
    if raw_hash:
        existing = coll.find_one({"raw_text_hash": raw_hash})
        if existing:
            return existing, {
                "is_duplicate": True,
                "score": 998.0,
                "matched_fields": ["raw_text_hash"],
                "important_fields": ["raw_text_hash"],
                "reason": "Exact same per-listing raw_text received again",
                "details": {
                    "raw_text_hash": raw_hash,
                },
            }

    return None, None


def _candidate_queries(listing: dict) -> list[dict]:
    queries: list[dict] = []

    property_type = listing.get("property_type")
    location = listing.get("location")
    bhk = listing.get("bhk")
    price = _to_float(listing.get("price_aed"))

    price_range = None
    if price is not None and price > 0:
        price_range = {
            "$gte": price * (1 - DUPLICATE_PRICE_TOLERANCE),
            "$lte": price * (1 + DUPLICATE_PRICE_TOLERANCE),
        }

    if location and price_range:
        queries.append({"location": location, "price_aed": price_range})

    if location and property_type:
        queries.append({"location": location, "property_type": property_type})

    if property_type and price_range:
        queries.append({"property_type": property_type, "price_aed": price_range})

    if property_type and bhk is not None:
        queries.append({"property_type": property_type, "bhk": bhk})

    if listing.get("fingerprint"):
        queries.append({"fingerprint": listing.get("fingerprint")})

    # Last safe fallback: same location only.
    if location:
        queries.append({"location": location})

    return queries


def _find_scored_duplicate(listing: dict, coll) -> tuple[dict | None, dict | None]:
    exact_doc, exact_info = _find_exact_raw_duplicate(listing, coll)
    if exact_doc:
        return exact_doc, exact_info

    seen_ids: set[ObjectId] = set()
    best_doc = None
    best_info = None

    for query in _candidate_queries(listing):
        cursor = coll.find(query).sort("created_at", -1).limit(DUPLICATE_CANDIDATE_LIMIT)

        for candidate in cursor:
            candidate_id = candidate.get("_id")
            if candidate_id in seen_ids:
                continue
            seen_ids.add(candidate_id)

            info = _score_duplicate_candidate(listing, candidate)
            if not info.get("is_duplicate"):
                continue

            if best_info is None or info["score"] > best_info["score"]:
                best_doc = candidate
                best_info = info

    return best_doc, best_info


def _should_update_field(field: str, old_value: object, new_value: object, old_doc: dict, new_doc: dict) -> bool:
    """
    Prevent worse duplicate parses from overwriting better stored data.
    """
    if not _value_present(new_value):
        return False

    if not _value_present(old_value):
        return True

    # Do not overwrite price with a very different value.
    if field in ("price_aed", "price_min_aed", "price_max_aed", "price_per_sqft_aed"):
        return _numbers_within_percent(old_value, new_value, DUPLICATE_PRICE_TOLERANCE)

    # Do not overwrite sizes with very different values.
    if field in ("sqft", "plot_sqft"):
        return _numbers_within_percent(old_value, new_value, DUPLICATE_SIZE_TOLERANCE)

    # Do not overwrite BHK if different.
    if field == "bhk":
        return old_value == new_value

    # Location safety: don't overwrite resolved/high-confidence location with unresolved/worse one.
    if field.startswith("location") or field == "location_coords":
        old_unresolved = old_doc.get("location_unresolved") is True
        new_unresolved = new_doc.get("location_unresolved") is True

        if new_unresolved and not old_unresolved:
            return False

        old_conf = _to_float(old_doc.get("location_confidence")) or 0.0
        new_conf = _to_float(new_doc.get("location_confidence")) or 0.0

        if old_conf and new_conf and new_conf + 0.05 < old_conf:
            return False

    return True


def _update_existing_duplicate(existing: dict, new_listing: dict, duplicate_info: dict, coll) -> dict:
    """
    Safely update old duplicate listing with latest metadata and safe non-null fields.
    Also records duplicate history and field overwrite history.
    """
    now = _now_utc()
    set_doc: dict = {
        "updated_at": now,
        "last_duplicate_at": now,
        "last_duplicate_reason": duplicate_info.get("reason"),
    }

    history_entries: list[dict] = []

    metadata_fields = [
        "wa_message_id",
        "wa_phone_number",
        "wa_received_at",
        "wa_sent_by",
        "customer_message",
        "tag",
        "source_message_hash",
        "source_message_snippet",
        "source_listing_index",
        "source_listing_count",
        "raw_text_hash",
    ]

    safe_data_fields = [
        "raw_text",
        "broker",
        "property_type",
        "location_raw",
        "location_hint",
        "location_resolution",
        "location",
        "location_level",
        "location_city",
        "location_community",
        "location_subcommunity",
        "location_property",
        "location_confidence",
        "location_resolution_path",
        "location_unresolved",
        "location_coords",
        "price_aed",
        "price_min_aed",
        "price_max_aed",
        "price_per_sqft_aed",
        "bhk",
        "sqft",
        "plot_sqft",
        "is_ready",
        "handover_year",
        "payment_plan",
        "furnishing",
        "amenities",
        "is_distress",
        "is_mortgage",
        "is_cash",
        "sent_by",
        "notes",
    ]

    updated_fields: list[str] = []

    # Metadata: update if latest value is present.
    for field in metadata_fields:
        new_value = new_listing.get(field)
        if not _value_present(new_value):
            continue

        old_value = existing.get(field)
        if old_value != new_value:
            set_doc[field] = new_value
            updated_fields.append(field)

            if _value_present(old_value):
                history_entries.append({
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed_at": now,
                    "reason": "duplicate_metadata_update",
                })

    # Property data: update only when safe.
    for field in safe_data_fields:
        new_value = new_listing.get(field)
        old_value = existing.get(field)

        if not _should_update_field(field, old_value, new_value, existing, new_listing):
            continue

        if old_value != new_value:
            set_doc[field] = new_value
            updated_fields.append(field)

            if _value_present(old_value):
                history_entries.append({
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "changed_at": now,
                    "reason": "duplicate_safe_update",
                })

    merged_for_fingerprint = {**existing, **set_doc}
    set_doc["fingerprint"] = _build_fingerprint(merged_for_fingerprint)

    duplicate_message = {
        "received_at": new_listing.get("wa_received_at"),
        "message_id": new_listing.get("wa_message_id"),
        "phone_number": new_listing.get("wa_phone_number"),
        "source_message_hash": new_listing.get("source_message_hash"),
        "source_listing_index": new_listing.get("source_listing_index"),
        "raw_text_hash": new_listing.get("raw_text_hash"),
        "detected_at": now,
        "reason": duplicate_info.get("reason"),
        "matched_fields": duplicate_info.get("matched_fields", []),
        "important_fields": duplicate_info.get("important_fields", []),
    }

    update_doc: dict = {
        "$set": set_doc,
        "$inc": {"duplicate_count": 1},
        "$push": {
            "duplicate_messages": duplicate_message,
        },
    }

    if history_entries:
        update_doc["$push"]["field_history"] = {"$each": history_entries}

    coll.update_one({"_id": existing["_id"]}, update_doc)

    return {
        **duplicate_info,
        "existing_id": str(existing["_id"]),
        "updated_fields": updated_fields,
        "history_entries_added": len(history_entries),
    }


def _is_duplicate(listing: dict, coll_name: str, enabled: bool) -> tuple[dict | None, dict | None]:
    """
    Return (existing_duplicate_document, duplicate_info) if duplicate found.
    """
    if not enabled:
        return None, None

    coll = _collection(coll_name)
    existing, duplicate_info = _find_scored_duplicate(listing, coll)

    if not existing:
        return None, None

    duplicate_info = _update_existing_duplicate(existing, listing, duplicate_info or {}, coll)

    msg = (
        f"[DB] Duplicate detected: existing={duplicate_info.get('existing_id')} | "
        f"reason={duplicate_info.get('reason')} | "
        f"fields={duplicate_info.get('matched_fields')}"
    )
    print(msg)

    try:
        from core.logger import log_event
        log_event("duplicate_detected", "info", "database", msg, {
            "existing_id": duplicate_info.get("existing_id"),
            "transaction": listing.get("transaction"),
            "location": listing.get("location"),
            "price_aed": listing.get("price_aed"),
            "bhk": listing.get("bhk"),
            "property_type": listing.get("property_type"),
            "matched_fields": duplicate_info.get("matched_fields"),
            "important_fields": duplicate_info.get("important_fields"),
            "updated_fields": duplicate_info.get("updated_fields"),
            "reason": duplicate_info.get("reason"),
        })
    except Exception:
        pass

    return existing, duplicate_info

# ── Insert ─────────────────────────────────────────────────────────────────────

def old_insert_listing(listing: dict) -> ObjectId | None:
    """
    Insert a listing into the correct collection.
    Returns existing _id if duplicate detected.
    """
    original_listing = listing
    listing = listing.copy()
    _apply_location_metadata(listing)
    listing["created_at"] = datetime.now(timezone.utc)
    listing["matched"] = False
    listing["match_id"] = None
    listing["fingerprint"] = _build_fingerprint(listing)
    listing.setdefault("customer_message", False)
    listing.setdefault("tag", None)
    

    transaction = listing.get("transaction", "sell").lower()
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL

    # dedupe_enabled = DUPLICATE_DETECTION_BUY if transaction == "buy" else DUPLICATE_DETECTION_SELL
    dedupe_enabled = False

    duplicate_doc = _is_duplicate(listing, coll_name, dedupe_enabled)
    if duplicate_doc:
        existing_id = duplicate_doc.get("_id")
        if isinstance(existing_id, ObjectId):
            if isinstance(original_listing, dict):
                original_listing["_duplicate"] = True
                original_listing["_id"] = existing_id
            return str(existing_id)
        return None

    result = _collection(coll_name).insert_one(listing)
    return str(result.inserted_id)

def insert_listing(listing: dict) -> ObjectId | None:
    """
    Insert a listing into the correct collection.

    If duplicate:
    - do not insert a new listing
    - safely update the old listing
    - set duplicate metadata on the original listing dict
    - return existing _id so matching can still run
    """
    original_listing = listing
    listing = listing.copy()

    _apply_location_metadata(listing)
    _prepare_duplicate_metadata(listing)

    listing["created_at"] = _now_utc()
    listing["matched"] = False
    listing["match_id"] = None
    listing.setdefault("customer_message", False)
    listing.setdefault("tag", None)

    transaction = (listing.get("transaction") or "sell").lower().strip()
    listing["transaction"] = transaction

    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL

    listing["fingerprint"] = _build_fingerprint(listing)

    dedupe_enabled = (
        DUPLICATE_DETECTION_BUY
        if transaction == "buy"
        else DUPLICATE_DETECTION_SELL
    )

    duplicate_doc, duplicate_info = _is_duplicate(listing, coll_name, dedupe_enabled)

    if duplicate_doc:
        existing_id = duplicate_doc.get("_id")
        if isinstance(existing_id, ObjectId):
            if isinstance(original_listing, dict):
                original_listing["_duplicate"] = True
                original_listing["_duplicate_info"] = duplicate_info or {}
                original_listing["_id"] = existing_id
            return str(existing_id)
        return None

    result = _collection(coll_name).insert_one(listing)
    return str(result.inserted_id)

def insert_many_listings(listings: list[dict]) -> tuple[list[ObjectId], int]:
    """
    Batch insert. Returns (list_of_inserted_ids, duplicate_count).
    """
    ids = []
    dupes = 0
    for listing in listings:
        inserted_id = insert_listing(listing)
        if inserted_id:
            if isinstance(listing, dict) and listing.pop("_duplicate", False):
                dupes += 1
            else:
                ids.append(inserted_id)
        else:
            dupes += 1
    return ids, dupes


# ── Read ───────────────────────────────────────────────────────────────────────

def get_all_active(transaction: str) -> list[dict]:
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    return list(_collection(coll_name).find())


def get_active_filtered(transaction: str, property_type: str | None, bhk: int | None) -> list[dict]:
    """
    Indexed pre-filter: only return candidates matching type and BHK.
    Dramatically reduces Python-side comparisons.
    """
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    query: dict = {}

    if property_type:
        query["property_type"] = property_type
    if bhk is not None:
        query["bhk"] = bhk

    return list(_collection(coll_name).find(query))


def get_all(transaction: str) -> list[dict]:
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    return list(_collection(coll_name).find())


def get_all_matches() -> list[dict]:
    return list(_collection(COLLECTION_MATCHES).find())


def get_all_project_matches() -> list[dict]:
    return list(_collection(COLLECTION_PROJECT_MATCHES).find())

def _coerce_object_ids(values: list[object]) -> list[ObjectId]:
    """Convert mixed string/ObjectId values into valid ObjectIds."""
    ids: list[ObjectId] = []

    for value in values:
        if isinstance(value, ObjectId):
            ids.append(value)
            continue

        if isinstance(value, str):
            try:
                ids.append(ObjectId(value))
            except Exception:
                continue

    # remove duplicates while preserving order
    seen = set()
    unique_ids: list[ObjectId] = []
    for oid in ids:
        if oid in seen:
            continue
        seen.add(oid)
        unique_ids.append(oid)

    return unique_ids


def get_matches_for_buy_ids(buy_ids: list[object]) -> list[dict]:
    """
    Return all historical broker matches for these buyer listing IDs.

    Used after duplicate detection:
    - matcher records only NEW pairs
    - this fetch returns OLD + NEW pairs for reply
    """
    ids = _coerce_object_ids(buy_ids)
    if not ids:
        return []

    return list(
        _collection(COLLECTION_MATCHES)
        .find({"buy_id": {"$in": ids}})
        .sort("matched_at", -1)
    )


def get_matches_for_sell_ids(sell_ids: list[object]) -> list[dict]:
    """
    Return all historical broker matches for these seller listing IDs.
    """
    ids = _coerce_object_ids(sell_ids)
    if not ids:
        return []

    return list(
        _collection(COLLECTION_MATCHES)
        .find({"sell_id": {"$in": ids}})
        .sort("matched_at", -1)
    )


def get_project_matches_for_buy_ids(buy_ids: list[object]) -> list[dict]:
    """
    Return all historical project matches for these buyer listing IDs.
    """
    ids = _coerce_object_ids(buy_ids)
    if not ids:
        return []

    return list(
        _collection(COLLECTION_PROJECT_MATCHES)
        .find({"buy_id": {"$in": ids}})
        .sort("matched_at", -1)
    )

def get_all_projects() -> list[dict]:
    return list(_collection(COLLECTION_PROJECTS).find())


def _build_project_fingerprint(project: dict) -> str:
    name = str(project.get("ProjectName") or "")
    developer = str(project.get("Developer") or "")
    area = str(project.get("AreaName") or "")
    raw = f"{name}{developer}{area}"
    return hashlib.md5(raw.encode()).hexdigest()


def insert_project(project: dict) -> str | ObjectId:
    project = project.copy()
    project["project_fingerprint"] = _build_project_fingerprint(project)

    exists = _collection(COLLECTION_PROJECTS).find_one({
        "project_fingerprint": project["project_fingerprint"],
    })
    if exists:
        return "duplicate"

    result = _collection(COLLECTION_PROJECTS).insert_one(project)
    return str(result.inserted_id)


# ── Historical Match Check ─────────────────────────────────────────────────────

def already_matched_pair(buy_id: ObjectId, sell_id: ObjectId) -> bool:
    """
    Check if this buy/sell pair has already been matched before.
    Prevents repeated matching of the same pair on re-runs.
    """
    exists = _collection(COLLECTION_MATCHES).find_one({
        "buy_id": buy_id,
        "sell_id": sell_id,
    })
    return exists is not None


def already_matched_project_pair(buy_id: ObjectId, project_id: ObjectId) -> bool:
    exists = _collection(COLLECTION_PROJECT_MATCHES).find_one({
        "buy_id": buy_id,
        "project_id": project_id,
    })
    return exists is not None


# ── Record Match ───────────────────────────────────────────────────────────────

def record_match(buy_id: ObjectId, sell_id: ObjectId, score: float, reasons: list[str], delete_after: bool = False):
    """Save match and mark (or delete) both listings."""
    db = get_db()

    # Guard: don't re-record an existing pair
    if already_matched_pair(buy_id, sell_id):
        print(f"[DB] Pair already matched: {buy_id} / {sell_id} — skipping.")
        return None

    buy_doc = db[COLLECTION_BUY].find_one({"_id": buy_id})
    sell_doc = db[COLLECTION_SELL].find_one({"_id": sell_id})

    match_doc = {
        "buy_id": buy_id,
        "sell_id": sell_id,
        "match_score": round(score, 4),
        "match_reasons": reasons,
        "buy_snapshot": _strip_meta(buy_doc),
        "sell_snapshot": _strip_meta(sell_doc),
        "matched_at": datetime.now(timezone.utc),
    }

    match_result = db[COLLECTION_MATCHES].insert_one(match_doc)
    try:
        from core.logger import log_event
        log_event("match_recorded", "info", "database",
            f"Match recorded: score={score:.2f} buy={buy_id} sell={sell_id}",
            {"buy_id": str(buy_id), "sell_id": str(sell_id),
             "score": round(score, 4), "reasons": reasons})
    except Exception:
        pass
    match_id = match_result.inserted_id

    if delete_after:
        db[COLLECTION_BUY].delete_one({"_id": buy_id})
        db[COLLECTION_SELL].delete_one({"_id": sell_id})
    else:
        db[COLLECTION_BUY].update_one({"_id": buy_id}, {"$set": {"matched": True, "match_id": match_id}})
        db[COLLECTION_SELL].update_one({"_id": sell_id}, {"$set": {"matched": True, "match_id": match_id}})

    return match_id


def record_project_match(
    buy_id: ObjectId,
    project_id: ObjectId,
    score: float,
    reasons: list[str],
    buy_snapshot: dict,
    project_snapshot: dict,
) -> ObjectId | None:
    if already_matched_project_pair(buy_id, project_id):
        return None

    doc = {
        "buy_id": buy_id,
        "project_id": project_id,
        "match_score": round(score, 4),
        "match_reasons": reasons,
        "buy_snapshot": buy_snapshot,
        "project_snapshot": project_snapshot,
        "buy_broker": (buy_snapshot or {}).get("broker") or {},
        "matched_at": datetime.now(timezone.utc),
    }

    result = _collection(COLLECTION_PROJECT_MATCHES).insert_one(doc)
    return str(result.inserted_id)


def _strip_meta(doc: dict) -> dict:
    if doc is None:
        return {}
    return {k: v for k, v in doc.items() if k not in ("_id", "matched", "match_id", "created_at", "fingerprint")}


# ── Utility ────────────────────────────────────────────────────────────────────

def count_listings():
    db = get_db()
    return {
        "buy": db[COLLECTION_BUY].count_documents({}),
        "sell": db[COLLECTION_SELL].count_documents({}),
        "buy_unmatched": db[COLLECTION_BUY].count_documents({"matched": False}),
        "sell_unmatched": db[COLLECTION_SELL].count_documents({"matched": False}),
        "matches": db[COLLECTION_MATCHES].count_documents({}),
    }


def dedupe_collection(transaction: str) -> dict:
    """
    Retroactively remove duplicate listings from an existing collection.
    Keeps the OLDEST document for each fingerprint (earliest inserted).
    Returns {"scanned": N, "removed": M}.
    Run once to clean up duplicates accumulated before fingerprint detection.
    """
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    coll = _collection(coll_name)

    all_docs = list(coll.find({}, {
        "_id": 1, "property_type": 1, "location": 1,
        "bhk": 1, "price_aed": 1, "sqft": 1, "transaction": 1
    }))

    seen: dict = {}     # fingerprint -> oldest _id kept
    to_delete: list = []

    for doc in all_docs:
        fp = _build_fingerprint(doc)
        if fp in seen:
            to_delete.append(doc["_id"])
        else:
            seen[fp] = doc["_id"]
            # Backfill fingerprint field if missing from older docs
            if "fingerprint" not in doc:
                coll.update_one({"_id": doc["_id"]}, {"$set": {"fingerprint": fp}})

    if to_delete:
        result = coll.delete_many({"_id": {"$in": to_delete}})
        deleted = result.deleted_count
    else:
        deleted = 0

    return {"scanned": len(all_docs), "removed": deleted}

def old_store_raw_message(payload: dict) -> str:
    """Store every incoming raw message before any processing."""
    payload = payload.copy()
    payload["stored_at"] = datetime.now(timezone.utc).isoformat()
    result = get_db()["raw_messages"].insert_one(payload)
    return str(result.inserted_id)

def store_raw_message(payload: dict) -> str:
    """Store every incoming raw message before any processing."""
    payload = payload.copy()
    payload["stored_at"] = _now_utc().isoformat()

    raw_text = payload.get("raw_message")
    if raw_text:
        payload["raw_message_hash"] = _hash_text(raw_text)

    result = get_db()["raw_messages"].insert_one(payload)
    return str(result.inserted_id)

def update_listing_tag(wa_message_id: str, tag: str) -> bool:
    """
    Find a listing by wa_message_id across buy and sell collections and update its tag.
    Returns True if a document was updated, False if not found.
    """
    db = get_db()
    for coll_name in (COLLECTION_BUY, COLLECTION_SELL):
        result = db[coll_name].update_one(
            {"wa_message_id": wa_message_id},
            {"$set": {"tag": tag}},
        )
        if result.matched_count > 0:
            return True
    return False

def backfill_new_fields() -> dict:
    """
    One-time migration: sets customer_message=False and tag=None on all existing
    documents that are missing these fields. Listings with a wa_message_id get
    customer_message=True instead.
    """
    db = get_db()
    stats = {}
    for coll_name in (COLLECTION_BUY, COLLECTION_SELL):
        coll = db[coll_name]
        # Docs with wa_message_id → customer_message True
        r1 = coll.update_many(
            {"wa_message_id": {"$exists": True}, "customer_message": {"$exists": False}},
            {"$set": {"customer_message": True, "tag": None}},
        )
        # Docs without wa_message_id → customer_message False
        r2 = coll.update_many(
            {"wa_message_id": {"$exists": False}, "customer_message": {"$exists": False}},
            {"$set": {"customer_message": False, "tag": None}},
        )
        # Docs that already have customer_message but missing tag
        r3 = coll.update_many(
            {"tag": {"$exists": False}},
            {"$set": {"tag": None}},
        )
        stats[coll_name] = {
            "with_wa_id_updated": r1.modified_count,
            "without_wa_id_updated": r2.modified_count,
            "tag_backfilled": r3.modified_count,
        }
    return stats

def clear_all():
    """⚠️ Drops all data. Use only in testing."""
    db = get_db()
    db[COLLECTION_BUY].drop()
    db[COLLECTION_SELL].drop()
    db[COLLECTION_MATCHES].drop()
    db[COLLECTION_PROJECTS].drop()
    db[COLLECTION_PROJECT_MATCHES].drop()
    db["raw_messages"].drop()
    db[COLLECTION_LOGS].drop()
    print("[DB] All collections cleared.")