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

from config import (
    MONGO_URI, MONGO_DB_NAME,
    COLLECTION_BUY, COLLECTION_SELL, COLLECTION_MATCHES,
    DUPLICATE_DETECTION, DUPLICATE_PRICE_TOLERANCE,
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

    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("sell_id", ASCENDING)])
    db[COLLECTION_MATCHES].create_index([("buy_id", ASCENDING), ("sell_id", ASCENDING)], unique=True)


def _collection(name: str):
    return get_db()[name]


# ── Fingerprint / Duplicate Detection ─────────────────────────────────────────

def _build_fingerprint(listing: dict) -> str:
    """
    Build a hash from the listing's key identity fields.
    Two listings with the same fingerprint are considered duplicates.
    Price is rounded to nearest 50K to handle minor reposts.
    """
    price = listing.get("price_aed") or 0
    # Round price to nearest 50,000 to tolerate trivial reprice changes
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


def _is_duplicate(listing: dict, coll_name: str) -> bool:
    """Check if a near-identical listing already exists in the collection."""
    if not DUPLICATE_DETECTION:
        return False

    fp = _build_fingerprint(listing)
    exists = _collection(coll_name).find_one({"fingerprint": fp})
    if exists:
        print(f"[DB] Duplicate detected — skipping listing: {listing.get('location')} {listing.get('bhk')}BR {listing.get('price_aed')}")
        return True
    return False


# ── Insert ─────────────────────────────────────────────────────────────────────

def insert_listing(listing: dict) -> ObjectId | None:
    """
    Insert a listing into the correct collection.
    Returns None if duplicate detected.
    """
    listing = listing.copy()
    listing["created_at"] = datetime.now(timezone.utc)
    listing["matched"] = False
    listing["match_id"] = None
    listing["fingerprint"] = _build_fingerprint(listing)

    transaction = listing.get("transaction", "sell").lower()
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL

    if _is_duplicate(listing, coll_name):
        return None

    result = _collection(coll_name).insert_one(listing)
    return result.inserted_id


def insert_many_listings(listings: list[dict]) -> tuple[list[ObjectId], int]:
    """
    Batch insert. Returns (list_of_inserted_ids, duplicate_count).
    """
    ids = []
    dupes = 0
    for listing in listings:
        inserted_id = insert_listing(listing)
        if inserted_id:
            ids.append(inserted_id)
        else:
            dupes += 1
    return ids, dupes


# ── Read ───────────────────────────────────────────────────────────────────────

def get_unmatched(transaction: str) -> list[dict]:
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    return list(_collection(coll_name).find({"matched": False}))


def get_unmatched_filtered(transaction: str, property_type: str | None, bhk: int | None) -> list[dict]:
    """
    Indexed pre-filter: only return candidates matching type and BHK.
    Dramatically reduces Python-side comparisons.
    """
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    query: dict = {"matched": False}

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


def clear_all():
    """⚠️ Drops all data. Use only in testing."""
    db = get_db()
    db[COLLECTION_BUY].drop()
    db[COLLECTION_SELL].drop()
    db[COLLECTION_MATCHES].drop()
    print("[DB] All collections cleared.")