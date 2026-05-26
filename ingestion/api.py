"""
api.py
FastAPI server for ingesting and matching real estate messages.
"""

import os
import tempfile
from datetime import datetime
from typing import Any

from bson import ObjectId
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import (
    API_KEY,
    COLLECTION_BUY,
    COLLECTION_SELL,
    WA_BUY_MATCH_MESSAGE,
    WA_FIELD_MESSAGE_ID,
    WA_FIELD_PHONE_NUMBER,
    WA_FIELD_RAW_MESSAGE,
    WA_NO_MATCH_MESSAGE,
    WA_NOTIFY_BOTH_SIDES,
    WA_SELL_MATCH_MESSAGE,
    WA_STORED_MESSAGE_ID,
    WA_STORED_PHONE_NUMBER,
    WA_STORED_RECEIVED_AT,
    WA_TIMESTAMP_FIELD,
)
from core.database import (
    insert_listing,
    insert_many_listings,
    count_listings,
    get_all_matches,
    get_db,
)
from core.matcher import run_matching
from ingestion.parser import parse_text_message, parse_image, is_real_estate_message
from location.resolver import resolve_location

app = FastAPI(title="Matcher API")


@app.middleware("http")
async def api_key_auth(request: Request, call_next):

    PUBLIC_PATHS = {
        "/",
        "/docs",
        "/openapi.json",
        "/favicon.ico"
    }

    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")

    if api_key != API_KEY:
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"}
        )

    return await call_next(request)


class TextIngestRequest(BaseModel):
    message: str


class WhatsAppIngestRequest(BaseModel):
    raw_message: str | None = Field(default=None, alias=WA_FIELD_RAW_MESSAGE)
    message_id: str = Field(alias=WA_FIELD_MESSAGE_ID)
    phone_number: str = Field(alias=WA_FIELD_PHONE_NUMBER)

    model_config = {
        "populate_by_name": True,
        "extra": "forbid",
    }


def _serialize(value: Any):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def _collect_match_docs(match_ids: list[ObjectId]) -> list[dict]:
    if not match_ids:
        return []
    match_id_set = set(match_ids)
    return [m for m in get_all_matches() if m.get("_id") in match_id_set]


def _format_matches(match_docs: list[dict]) -> list[dict]:
    formatted = []
    for doc in match_docs:
        buy_snapshot = doc.get("buy_snapshot") or {}
        sell_snapshot = doc.get("sell_snapshot") or {}
        formatted.append({
            "match_id": _serialize(doc.get("_id")),
            "score": doc.get("match_score"),
            "reasons": doc.get("match_reasons") or [],
            "buy_broker": buy_snapshot.get("broker"),
            "sell_broker": sell_snapshot.get("broker"),
            "buy_snapshot": _serialize(buy_snapshot),
            "sell_snapshot": _serialize(sell_snapshot),
        })
    return formatted


def _get_listing_by_id(listing_id: ObjectId, transaction: str) -> dict | None:
    db = get_db()
    coll_name = COLLECTION_BUY if transaction == "buy" else COLLECTION_SELL
    return db[coll_name].find_one({"_id": listing_id})


def _safe_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _safe_bhk(value: object) -> str:
    if value is None:
        return "N/A"
    try:
        return str(int(value))
    except Exception:
        return str(value)


def _apply_location_resolution(listings: list[dict]) -> list[dict]:
    resolved = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        raw = listing.get("location_raw")
        if not raw:
            raw = listing.get("location") or ""
        listing["location_raw"] = raw

        hint = listing.get("location_hint")
        if not isinstance(hint, dict):
            hint = {}
        listing["location_hint"] = hint

        listing["location_resolution"] = resolve_location(
            location_raw=raw,
            location_hint=hint or {},
        )
        resolved.append(listing)
    return resolved

@app.get("/")
async def root():
    return {"message": "Matcher API is running"}

@app.post("/ingest/text")
async def ingest_text(payload: TextIngestRequest):
    if not is_real_estate_message(payload.message):
        return {"filtered": True, "reason": "not a real estate message"}

    listings = parse_text_message(payload.message)
    listings = _apply_location_resolution(listings)
    inserted_ids, dupes = insert_many_listings(listings)
    matches = run_matching()

    match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])

    return {
        "filtered": False,
        "inserted": len(inserted_ids),
        "duplicates_skipped": dupes,
        "listings": listings,
        "matches": _format_matches(match_docs),
    }


@app.post("/ingest/image")
async def ingest_image(file: UploadFile = File(...)):
    temp_path = None
    try:
        suffix = os.path.splitext(file.filename or "")[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            content = await file.read()
            tmp.write(content)

        listings = parse_image(temp_path)
        listings = _apply_location_resolution(listings)
        inserted_ids, dupes = insert_many_listings(listings)
        matches = run_matching()

        match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])

        return {
            "filtered": False,
            "inserted": len(inserted_ids),
            "duplicates_skipped": dupes,
            "listings": listings,
            "matches": _format_matches(match_docs),
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/ingest/whatsapp")
async def ingest_whatsapp(payload: WhatsAppIngestRequest):
    received_at = datetime.utcnow().isoformat() + "Z"

    try:
        raw_message = payload.raw_message or ""
        if not raw_message.strip():
            return JSONResponse(status_code=422, content={"detail": "raw_message is required"})

        message_id = payload.message_id
        phone_number = payload.phone_number

        listings = parse_text_message(raw_message)
        if not listings:
            return {
                WA_FIELD_MESSAGE_ID: message_id,
                WA_FIELD_PHONE_NUMBER: phone_number,
                WA_TIMESTAMP_FIELD: received_at,
                "inserted": 0,
                "duplicates_skipped": 0,
                "listings_parsed": 0,
                "match_found": False,
                "reply_message": WA_NO_MATCH_MESSAGE,
                "reply_phone_number": None,
                "reply_listing_date": None,
                "match_details": None,
                "second_reply": None,
            }

        listings = _apply_location_resolution(listings)

        for listing in listings:
            if not isinstance(listing, dict):
                continue
            listing[WA_STORED_MESSAGE_ID] = message_id
            listing[WA_STORED_PHONE_NUMBER] = phone_number
            listing[WA_STORED_RECEIVED_AT] = received_at

        inserted_ids: list[ObjectId] = []
        dupes = 0
        for listing in listings:
            inserted_id = insert_listing(listing)
            if inserted_id:
                inserted_ids.append(inserted_id)
            else:
                dupes += 1

        matches = run_matching()
        match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])

        inserted_id_set = set(inserted_ids)
        relevant_matches: list[dict] = []

        for doc in match_docs:
            buy_id = doc.get("buy_id")
            sell_id = doc.get("sell_id")
            if buy_id not in inserted_id_set and sell_id not in inserted_id_set:
                continue

            match_details = {
                "match_id": _serialize(doc.get("_id")),
                "score": doc.get("match_score"),
                "reasons": doc.get("match_reasons") or [],
                "buy_snapshot": _serialize(doc.get("buy_snapshot") or {}),
                "sell_snapshot": _serialize(doc.get("sell_snapshot") or {}),
            }

            incoming_is_buy = buy_id in inserted_id_set
            relevant_matches.append({
                **match_details,
                "_incoming_is_buy": incoming_is_buy,
                "_buy_id": buy_id,
                "_sell_id": sell_id,
            })

        if not relevant_matches:
            return {
                WA_FIELD_MESSAGE_ID: message_id,
                WA_FIELD_PHONE_NUMBER: phone_number,
                WA_TIMESTAMP_FIELD: received_at,
                "inserted": len(inserted_ids),
                "duplicates_skipped": dupes,
                "listings_parsed": len(listings),
                "match_found": False,
                "reply_message": WA_NO_MATCH_MESSAGE,
                "reply_phone_number": None,
                "reply_listing_date": None,
                "match_details": None,
                "second_reply": None,
            }

        def _reply_payload(match_info: dict) -> dict:
            incoming_is_buy = match_info.get("_incoming_is_buy", True)
            buy_snapshot = match_info.get("buy_snapshot") or {}
            sell_snapshot = match_info.get("sell_snapshot") or {}
            score = match_info.get("score") or 0.0

            if incoming_is_buy:
                opposite_snapshot = sell_snapshot
                opposite_doc = _get_listing_by_id(match_info.get("_sell_id"), "sell")
                location = opposite_snapshot.get("location", "N/A")
                price = _safe_int(opposite_snapshot.get("price_aed", 0))
                bhk = _safe_bhk(opposite_snapshot.get("bhk", "N/A"))
                property_type = opposite_snapshot.get("property_type", "N/A")
                broker = opposite_snapshot.get("broker", {}) or {}
                broker_name = broker.get("name", "N/A")
                broker_phone = broker.get("phone", "N/A")
                message = WA_BUY_MATCH_MESSAGE.format(
                    location=location,
                    price=price,
                    bhk=bhk,
                    property_type=property_type,
                    broker_name=broker_name,
                    broker_phone=broker_phone,
                    score=score,
                )
            else:
                opposite_snapshot = buy_snapshot
                opposite_doc = _get_listing_by_id(match_info.get("_buy_id"), "buy")
                location = opposite_snapshot.get("location", "N/A")
                budget = _safe_int(opposite_snapshot.get("price_aed", 0))
                bhk = _safe_bhk(opposite_snapshot.get("bhk", "N/A"))
                property_type = opposite_snapshot.get("property_type", "N/A")
                broker = opposite_snapshot.get("broker", {}) or {}
                broker_name = broker.get("name", "N/A")
                broker_phone = broker.get("phone", "N/A")
                message = WA_SELL_MATCH_MESSAGE.format(
                    location=location,
                    budget=budget,
                    bhk=bhk,
                    property_type=property_type,
                    broker_name=broker_name,
                    broker_phone=broker_phone,
                    score=score,
                )

            reply_phone = (opposite_snapshot.get("broker") or {}).get("phone")
            reply_date = None
            if opposite_doc:
                reply_date = _serialize(opposite_doc.get("created_at"))

            return {
                "reply_message": message,
                "reply_phone_number": reply_phone,
                "reply_listing_date": reply_date,
            }

        def _second_reply_payload(match_info: dict) -> dict | None:
            if not WA_NOTIFY_BOTH_SIDES:
                return None

            incoming_is_buy = match_info.get("_incoming_is_buy", True)
            incoming_id = match_info.get("_buy_id") if incoming_is_buy else match_info.get("_sell_id")
            incoming_doc = _get_listing_by_id(incoming_id, "buy" if incoming_is_buy else "sell")

            incoming_snapshot = match_info.get("buy_snapshot") if incoming_is_buy else match_info.get("sell_snapshot")
            incoming_snapshot = incoming_snapshot or {}

            score = match_info.get("score") or 0.0

            if incoming_is_buy:
                location = incoming_snapshot.get("location", "N/A")
                price = _safe_int(incoming_snapshot.get("price_aed", 0))
                bhk = _safe_bhk(incoming_snapshot.get("bhk", "N/A"))
                property_type = incoming_snapshot.get("property_type", "N/A")
                broker = incoming_snapshot.get("broker", {}) or {}
                broker_name = broker.get("name", "N/A")
                broker_phone = broker.get("phone", "N/A")
                message = WA_BUY_MATCH_MESSAGE.format(
                    location=location,
                    price=price,
                    bhk=bhk,
                    property_type=property_type,
                    broker_name=broker_name,
                    broker_phone=broker_phone,
                    score=score,
                )
            else:
                location = incoming_snapshot.get("location", "N/A")
                budget = _safe_int(incoming_snapshot.get("price_aed", 0))
                bhk = _safe_bhk(incoming_snapshot.get("bhk", "N/A"))
                property_type = incoming_snapshot.get("property_type", "N/A")
                broker = incoming_snapshot.get("broker", {}) or {}
                broker_name = broker.get("name", "N/A")
                broker_phone = broker.get("phone", "N/A")
                message = WA_SELL_MATCH_MESSAGE.format(
                    location=location,
                    budget=budget,
                    bhk=bhk,
                    property_type=property_type,
                    broker_name=broker_name,
                    broker_phone=broker_phone,
                    score=score,
                )

            reply_date = None
            if incoming_doc:
                reply_date = _serialize(incoming_doc.get("created_at"))

            return {
                "reply_message": message,
                "reply_phone_number": phone_number,
                "reply_listing_date": reply_date,
            }

        best_match = max(relevant_matches, key=lambda m: m.get("score") or 0.0)
        reply_payload = _reply_payload(best_match)

        response = {
            WA_FIELD_MESSAGE_ID: message_id,
            WA_FIELD_PHONE_NUMBER: phone_number,
            WA_TIMESTAMP_FIELD: received_at,
            "inserted": len(inserted_ids),
            "duplicates_skipped": dupes,
            "listings_parsed": len(listings),
            "match_found": True,
            **reply_payload,
            "second_reply": None,
        }

        if len(relevant_matches) == 1:
            match_details = {
                k: v for k, v in relevant_matches[0].items() if not k.startswith("_")
            }
            response["match_details"] = match_details
        else:
            matches_payload = [
                {k: v for k, v in m.items() if not k.startswith("_")}
                for m in relevant_matches
            ]
            response["matches"] = matches_payload
            response["match_details"] = None

        second_reply = _second_reply_payload(best_match)
        if second_reply is not None:
            response["second_reply"] = second_reply

        return response

    except Exception as e:
        print(f"[API] WhatsApp ingest failed: {e}")
        return {
            WA_FIELD_MESSAGE_ID: payload.message_id,
            WA_FIELD_PHONE_NUMBER: payload.phone_number,
            WA_TIMESTAMP_FIELD: received_at,
            "inserted": 0,
            "duplicates_skipped": 0,
            "listings_parsed": 0,
            "match_found": False,
            "reply_message": WA_NO_MATCH_MESSAGE,
            "reply_phone_number": None,
            "reply_listing_date": None,
            "match_details": None,
            "second_reply": None,
        }


@app.get("/stats")
async def get_stats():
    return count_listings()


@app.get("/matches")
async def get_matches(unnotified_only: bool = False):
    matches = get_all_matches()
    return _serialize(matches)
