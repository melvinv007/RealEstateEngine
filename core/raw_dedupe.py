"""
raw_dedupe.py

Raw-message-level fuzzy duplicate detection. Checks the incoming raw text
against recently stored raw messages BEFORE calling the LLM parser. On a
high-confidence match, clones the previously parsed listing(s) instead of
re-parsing, saving the Gemini call.

Controlled entirely by RAW_DEDUPE_MODE in core/config.py — see that file for
the off/shadow/active semantics.
"""

import re
from typing import Any

from rapidfuzz import fuzz

from core.config import (
    RAW_DEDUPE_MODE,
    RAW_DEDUPE_TEXT_THRESHOLD,
    RAW_DEDUPE_REQUIRE_NUMERIC_MATCH,
    RAW_DEDUPE_SINGLE_LISTING_ONLY,
    RAW_DEDUPE_CANDIDATE_LOOKBACK,
)
from core.database import get_db, _normalise_text

_BUY_SIGNALS = [
    "looking for", "requirement", "wanted", "budget", "pre-approved",
    "investor looking", "ready to close", "mortgage buyer", "cash buyer",
    "client looking", "client need", "client require", "need apartment",
    "need villa", "need property", "seeking",
]
_SELL_SIGNALS = [
    "for sale", "selling", "asking price", "distress deal",
    "open for serious offers", "seller", "vacant", "ready to move",
    "handover", "resale",
]

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:m|million|k)\b")
_SENT_BY_RE = re.compile(r"(?:sent by|from)\s*[:\-]?\s*([A-Za-z][A-Za-z .]{1,40})", re.IGNORECASE)


def guess_transaction(text: str) -> str:
    """
    Cheap, no-LLM guess at buy/sell. Used ONLY to bucket the fuzzy search space
    so a buy message can never get fuzzy-matched against a sell message.
    """
    t = (text or "").lower()
    buy_hit = any(s in t for s in _BUY_SIGNALS)
    sell_hit = any(s in t for s in _SELL_SIGNALS)
    if buy_hit and not sell_hit:
        return "buy"
    if sell_hit and not buy_hit:
        return "sell"
    return "unknown"


def _numbers_consistent(a: str, b: str) -> bool:
    """
    Two messages can be 90%+ similar in raw text while differing only in price
    or BHK count ("budget 2M" vs "budget 2.5M"). This guards against reusing a
    parse whose numbers don't actually match the new message.
    """
    return set(_NUMBER_RE.findall(a or "")) == set(_NUMBER_RE.findall(b or ""))


def _looks_like_multi_listing(text: str) -> bool:
    amounts = _AMOUNT_RE.findall((text or "").lower())
    return len(set(amounts)) > 1


def extract_sent_by(text: str) -> str | None:
    match = _SENT_BY_RE.search(text or "")
    if not match:
        return None
    name = match.group(1).strip().rstrip(".:")
    return name or None


def find_raw_match(raw_text: str) -> dict | None:
    """
    Returns {"doc": ..., "score": ...} for the best matching stored raw
    message, scoped to the inferred transaction bucket, or None.
    """
    if RAW_DEDUPE_MODE == "off":
        return None

    new_norm = _normalise_text(raw_text)
    if not new_norm:
        return None

    if RAW_DEDUPE_SINGLE_LISTING_ONLY and _looks_like_multi_listing(raw_text):
        return None

    guessed_txn = guess_transaction(raw_text)

    query: dict = {"raw_message_normalized": {"$exists": True}}
    if guessed_txn != "unknown":
        query["dedupe_transaction"] = guessed_txn
    if RAW_DEDUPE_SINGLE_LISTING_ONLY:
        query["dedupe_listing_count"] = 1

    db = get_db()
    candidates = list(
        db["raw_messages"]
        .find(query)
        .sort("stored_at", -1)
        .limit(RAW_DEDUPE_CANDIDATE_LOOKBACK)
    )

    best, best_score = None, 0.0
    for cand in candidates:
        cand_norm = cand.get("raw_message_normalized") or ""
        if not cand_norm:
            continue
        score = fuzz.token_set_ratio(new_norm, cand_norm)
        if score > best_score:
            best, best_score = cand, score

    if not best or best_score < RAW_DEDUPE_TEXT_THRESHOLD:
        return None

    if RAW_DEDUPE_REQUIRE_NUMERIC_MATCH and not _numbers_consistent(raw_text, best.get("raw_message") or ""):
        return None

    return {"doc": best, "score": best_score, "guessed_transaction": guessed_txn}


def clone_listings_from_match(match: dict) -> list[dict] | None:
    """
    Build fresh listing dicts from a previous match's stored snapshots,
    stripped of identity/metadata fields. Caller is responsible for stamping
    new message_id/phone_number/wa_received_at/sent_by afterward.
    """
    snapshots = match["doc"].get("dedupe_listing_snapshots")
    if not snapshots:
        return None

    strip_fields = (
        "_id", "wa_message_id", "wa_phone_number", "wa_received_at", "wa_sent_by",
        "fingerprint", "created_at", "matched", "match_id",
        "source_message_hash", "source_message_snippet", "raw_text_hash",
        "duplicate_count", "duplicate_messages", "field_history",
    )

    cloned = []
    for snapshot in snapshots:
        new_listing = {**snapshot}
        for field in strip_fields:
            new_listing.pop(field, None)
        cloned.append(new_listing)

    return cloned