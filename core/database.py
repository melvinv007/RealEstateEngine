"""
database.py
MongoDB operations with:
- Duplicate fingerprint detection
- Historical match prevention
- Index creation for fast pre-filtering
"""

import hashlib
import json
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from bson import ObjectId

from core.config import (
    MONGO_URI, MONGO_DB_NAME,
    COLLECTION_BUY, COLLECTION_SELL, COLLECTION_MATCHES,
    COLLECTION_PROJECTS, COLLECTION_PROJECT_MATCHES,
    DUPLICATE_DETECTION_BUY, DUPLICATE_DETECTION_SELL, DUPLICATE_PRICE_TOLERANCE,
)

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
        coll.create_index([("location_coords", "2dsphere")])

    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("sell_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING), ("sell_id", ASCENDING)], unique=True)

    projects = db[COLLECTION_PROJECTS]
    projects.create_index([("location_coords", "2dsphere")])
    projects.create_index([("project_fingerprint", ASCENDING)], unique=True)

    project_matches = db[COLLECTION_PROJECT_MATCHES]
    project_matches.create_index([("buy_id", ASCENDING), ("project_id", ASCENDING)], unique=True)


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


# ── Fingerprint / Duplicate Detection ─────────────────────────────────────────

def _build_fingerprint(listing: dict) -> str:
    """
    Build a hash from the listing's key identity fields.
    Two listings with the same fingerprint are considered duplicates.
    Price is rounded to nearest DUPLICATE_PRICE_TOLERANCE to handle minor reposts.
    """
    price = listing.get("price_aed") or 0
    # Round price to nearest DUPLICATE_PRICE_TOLERANCE to tolerate trivial reprice changes
    price_bucket = round(price / DUPLICATE_PRICE_TOLERANCE) * DUPLICATE_PRICE_TOLERANCE

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


def _is_duplicate(listing: dict, coll_name: str, enabled: bool) -> dict | None:
    """Return the existing duplicate document if found; otherwise None."""
    if not enabled:
        return None

    fp = _build_fingerprint(listing)
    exists = _collection(coll_name).find_one({"fingerprint": fp})
    if exists:
        print(f"[DB] Duplicate detected — skipping listing: {listing.get('location')} {listing.get('bhk')}BR {listing.get('price_aed')}")
        return exists
    return None


# ── Insert ─────────────────────────────────────────────────────────────────────

def insert_listing(listing: dict) -> ObjectId | None:
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

def store_raw_message(payload: dict) -> str:
    """Store every incoming raw message before any processing."""
    payload = payload.copy()
    payload["stored_at"] = datetime.now(timezone.utc).isoformat()
    result = get_db()["raw_messages"].insert_one(payload)
    return str(result.inserted_id)

def clear_all():
    """⚠️ Drops all data. Use only in testing."""
    db = get_db()
    db[COLLECTION_BUY].drop()
    db[COLLECTION_SELL].drop()
    db[COLLECTION_MATCHES].drop()
    db[COLLECTION_PROJECTS].drop()
    db[COLLECTION_PROJECT_MATCHES].drop()
    print("[DB] All collections cleared.")