"""
api.py
FastAPI server for ingesting and matching real estate messages.
"""

import os
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import base64
from bson import ObjectId
from fastapi import FastAPI, File, UploadFile, Request, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import (
    API_KEY,
    COLLECTION_BUY,
    COLLECTION_SELL,
    WA_BROKER_BUY_TEMPLATE,
    WA_BROKER_SELL_TEMPLATE,
    WA_FIELD_MESSAGE_ID,
    WA_FIELD_PHONE_NUMBER,
    WA_FIELD_RAW_MESSAGE,
    WA_BUY_MATCH_HEADER_BROKER_ONLY,
    WA_BUY_MATCH_HEADER_PROJECT_ONLY,
    WA_BUY_MATCH_HEADER_BOTH,
    WA_NO_MATCH_BUY_MESSAGE,
    WA_NO_MATCH_SELL_MESSAGE,
    WA_PROJECT_TEMPLATE,
    WA_SELL_MATCH_HEADER,
    WA_STORED_MESSAGE_ID,
    WA_STORED_PHONE_NUMBER,
    WA_STORED_RECEIVED_AT,
    WA_TIMESTAMP_FIELD,
)
from core.database import (
    insert_listing,
    count_listings,
    get_all_matches,
    get_db,
    _build_fingerprint,
)
from core.pipeline_watcher import check_and_run_pipelines
from core.matcher import run_matching, run_project_matching, run_matching_for_sell
from ingestion.parser import parse_text_message, parse_image, is_real_estate_message
from location.resolver import resolve_location


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=check_and_run_pipelines, daemon=True).start()
    yield


app = FastAPI(title="Matcher API", lifespan=lifespan)


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


def _format_broker_sell_matches(match_docs: list[dict]) -> list[dict]:
    formatted = []
    for doc in match_docs:
        sell_snapshot = doc.get("sell_snapshot") or {}
        broker = sell_snapshot.get("broker") or {}
        formatted.append({
            "match_type": "broker_sell",
            "score": doc.get("match_score"),
            "reasons": doc.get("match_reasons") or [],
            "sell_broker": broker,
            "sell_snapshot": {
                "property_type": sell_snapshot.get("property_type"),
                "bhk": sell_snapshot.get("bhk"),
                "price_aed": sell_snapshot.get("price_aed"),
                "location": sell_snapshot.get("location"),
            },
        })
    return formatted


def _format_project_matches(project_matches: list[dict]) -> list[dict]:
    formatted = []
    for match in project_matches:
        formatted.append({
            "match_type": "project",
            "score": match.get("score"),
            "reasons": match.get("reasons") or [],
            "project_name": match.get("project_name"),
            "developer": match.get("developer"),
            "area": match.get("area"),
            "starting_price": match.get("starting_price"),
            "handover": match.get("handover"),
            "payment_plan": match.get("payment_plan"),
            "bedrooms_available": match.get("bedrooms_available"),
            "youtube_link": match.get("youtube_link"),
            "image_link": match.get("image_link"),
            "pdf_link": match.get("pdf_link"),
        })
    return formatted


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


def _format_bedrooms(value: object) -> str:
    if not value:
        return "N/A"
    if isinstance(value, (list, tuple)):
        labels = []
        for item in value:
            if item == 0:
                labels.append("Studio")
            else:
                labels.append(f"{item}BR")
        return ", ".join(labels) if labels else "N/A"
    return str(value)


def _best_buy_budget(snapshot: dict) -> object:
    for key in ("price_aed", "price_max_aed", "price_min_aed"):
        value = snapshot.get(key)
        if value is not None:
            return value
    return 0


def _build_reply_message(transaction: str, matches: list[dict]) -> str:
    if not matches:
        return WA_NO_MATCH_SELL_MESSAGE if transaction == "sell" else WA_NO_MATCH_BUY_MESSAGE

    broker_lines: list[str] = []
    project_lines: list[str] = []

    def _clean_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _format_bhk_value(value: object) -> str:
        if value is None:
            return ""
        try:
            num = int(value)
            if num == 0:
                return "Studio"
            return f"{num}BR"
        except Exception:
            text = str(value).strip()
            return text or ""

    def _format_price(value: object) -> str | None:
        if value is None:
            return None
        try:
            amount = float(value)
        except Exception:
            return None
        if amount <= 0:
            return None
        return f"AED {amount:,.0f}"

    def _format_contact(broker: dict) -> str | None:
        name = _clean_text((broker or {}).get("name"))
        phone = _clean_text((broker or {}).get("phone"))
        parts = [p for p in (name, phone) if p]
        if not parts:
            return None
        return "Contact: " + " ".join(parts)

    def _append_segment(segments: list[str], label: str, value: object) -> None:
        text = _clean_text(value)
        if text:
            segments.append(f"{label}{text}")

    def _format_project_bedrooms(value: object) -> str | None:
        if value is None:
            return None
        label = _format_bedrooms(value)
        label = _clean_text(label)
        if not label or label == "N/A":
            return None
        return label

    for match in matches:
        match_type = match.get("match_type")
        if transaction == "buy" and match_type == "broker_sell":
            snapshot = match.get("sell_snapshot") or {}
            broker = match.get("sell_broker") or {}
            segments: list[str] = []

            property_parts = []
            property_type = _clean_text(snapshot.get("property_type"))
            if property_type:
                property_parts.append(property_type)
            bhk_label = _format_bhk_value(snapshot.get("bhk"))
            if bhk_label:
                property_parts.append(bhk_label)
            location = _clean_text(snapshot.get("location"))
            if location:
                property_parts.append(f"at {location}")
            if property_parts:
                segments.append(" ".join(property_parts))

            price_text = _format_price(snapshot.get("price_aed"))
            if price_text:
                segments.append(price_text)

            contact_text = _format_contact(broker)
            if contact_text:
                segments.append(contact_text)

            if segments:
                broker_lines.append("[Broker Sell] " + " | ".join(segments))

        elif transaction == "sell" and match_type == "broker_buy":
            snapshot = match.get("buy_snapshot") or {}
            broker = match.get("buy_broker") or {}
            segments: list[str] = []

            property_parts = []
            property_type = _clean_text(snapshot.get("property_type"))
            if property_type:
                property_parts.append(property_type)
            bhk_label = _format_bhk_value(snapshot.get("bhk"))
            if bhk_label:
                property_parts.append(bhk_label)
            location = _clean_text(snapshot.get("location"))
            if location:
                property_parts.append(f"in {location}")
            if property_parts:
                segments.append("Looking for " + " ".join(property_parts))

            budget_text = _format_price(_best_buy_budget(snapshot))
            if budget_text:
                segments.append(f"Budget: {budget_text}")

            contact_text = _format_contact(broker)
            if contact_text:
                segments.append(contact_text)

            if segments:
                broker_lines.append("[Buyer] " + " | ".join(segments))

        elif transaction == "buy" and match_type == "project":
            segments: list[str] = []
            header_parts = []
            project_name = _clean_text(match.get("project_name"))
            if project_name:
                header_parts.append(project_name)
            developer = _clean_text(match.get("developer"))
            if developer:
                header_parts.append(f"by {developer}")
            area = _clean_text(match.get("area"))
            if area:
                header_parts.append(f"in {area}")
            if header_parts:
                segments.append(" ".join(header_parts))

            starting_price = _format_price(match.get("starting_price"))
            if starting_price:
                segments.append(f"Starting {starting_price}")

            bedrooms = _format_project_bedrooms(match.get("bedrooms_available"))
            if bedrooms:
                segments.append(bedrooms)

            _append_segment(segments, "Handover: ", match.get("handover"))
            _append_segment(segments, "", match.get("payment_plan"))
            _append_segment(segments, "PDF: ", match.get("pdf_link"))

            if segments:
                project_lines.append("[New Project] " + " | ".join(segments))

    if transaction == "sell":
        if not broker_lines:
            return WA_NO_MATCH_SELL_MESSAGE
        header = WA_SELL_MATCH_HEADER.format(n=len(broker_lines))
        return "\n".join([header] + broker_lines)

    if not broker_lines and not project_lines:
        return WA_NO_MATCH_BUY_MESSAGE

    nb, np = len(broker_lines), len(project_lines)
    if nb > 0 and np > 0:
        header = WA_BUY_MATCH_HEADER_BOTH.format(nb=nb, np=np)
    elif nb > 0:
        header = WA_BUY_MATCH_HEADER_BROKER_ONLY.format(n=nb)
    else:
        header = WA_BUY_MATCH_HEADER_PROJECT_ONLY.format(n=np)

    return "\n".join([header] + broker_lines + project_lines)


def _best_broker_phone(matches: list[dict]) -> str | None:
    broker_matches = [m for m in matches if m.get("match_type") == "broker_sell"]
    if not broker_matches:
        return None
    best = max(broker_matches, key=lambda m: m.get("score") or 0.0)
    broker = best.get("sell_broker") or {}
    return broker.get("phone")


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


def _find_existing_listing_by_fingerprint(listing: dict) -> dict | None:
    transaction = (listing.get("transaction") or "").lower()
    if transaction == "buy":
        coll_name = COLLECTION_BUY
    else:
        coll_name = COLLECTION_SELL

    fingerprint = listing.get("fingerprint") or _build_fingerprint(listing)
    return get_db()[coll_name].find_one({"fingerprint": fingerprint})


def _request_listing_ids(listings: list[dict], transaction: str) -> set[ObjectId]:
    request_ids: set[ObjectId] = set()
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        if (listing.get("transaction") or "").lower() != transaction:
            continue

        listing_id = listing.get("_id")
        if isinstance(listing_id, ObjectId):
            request_ids.add(listing_id)
            continue

        existing = _find_existing_listing_by_fingerprint(listing)
        if existing and isinstance(existing.get("_id"), ObjectId):
            request_ids.add(existing["_id"])

    return request_ids


@app.get("/")
async def root():
    return {"message": "Matcher API is running"}


@app.post("/ingest/text")
async def ingest_text(payload: TextIngestRequest):
    if not is_real_estate_message(payload.message):
        return {"filtered": True, "reason": "not a real estate message"}

    listings = parse_text_message(payload.message)
    if not listings:
        return {
            "filtered": False,
            WA_FIELD_MESSAGE_ID: None,
            WA_FIELD_PHONE_NUMBER: None,
            WA_TIMESTAMP_FIELD: None,
            "inserted": 0,
            "duplicates_skipped": 0,
            "listings": [],
            "match_found": False,
            "reply_message": WA_NO_MATCH_BUY_MESSAGE,
            "reply_phone_number": None,
            "matches": [],
        }
    listings = _apply_location_resolution(listings)
    inserted_ids: list[ObjectId] = []
    dupes = 0
    for listing in listings:
        inserted_id = insert_listing(listing)
        if inserted_id:
            listing["_id"] = inserted_id
            if listing.pop("_duplicate", False):
                dupes += 1
            else:
                inserted_ids.append(inserted_id)
        else:
            dupes += 1

    request_buy_ids = _request_listing_ids(listings, "buy")
    has_buy = any(
        (listing.get("transaction") or "").lower() == "buy"
        for listing in listings
        if isinstance(listing, dict)
    )

    combined_matches: list[dict] = []
    match_found = False
    if has_buy:
        matches = run_matching()
        project_matches = run_project_matching()
        match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])
        relevant_docs = [doc for doc in match_docs if doc.get("buy_id") in request_buy_ids]
        relevant_projects = [
            match for match in project_matches
            if match.get("buy_id") in request_buy_ids
        ]
        combined_matches = _format_broker_sell_matches(relevant_docs) + _format_project_matches(relevant_projects)
        combined_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)

        match_found = bool(combined_matches)
        reply_message = _build_reply_message("buy", combined_matches)
        reply_phone_number = _best_broker_phone(combined_matches)
    else:
        sell_matches: list[dict] = []
        for listing in listings:
            if listing.get("transaction") != "sell":
                continue
            sell_doc = listing if isinstance(listing.get("_id"), ObjectId) else _find_existing_listing_by_fingerprint(listing) or listing
            sell_matches.extend(run_matching_for_sell(sell_doc))

        sell_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)
        combined_matches = sell_matches
        match_found = bool(sell_matches)
        reply_message = _build_reply_message("sell", sell_matches)
        reply_phone_number = None

    return {
        "filtered": False,
        WA_FIELD_MESSAGE_ID: None,
        WA_FIELD_PHONE_NUMBER: None,
        WA_TIMESTAMP_FIELD: None,
        "inserted": len(inserted_ids),
        "duplicates_skipped": dupes,
        "listings": listings,
        "match_found": match_found,
        "reply_message": reply_message,
        "reply_phone_number": reply_phone_number,
        "matches": combined_matches,
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
        if not listings:
            return {
                "filtered": False,
                WA_FIELD_MESSAGE_ID: None,
                WA_FIELD_PHONE_NUMBER: None,
                WA_TIMESTAMP_FIELD: None,
                "inserted": 0,
                "duplicates_skipped": 0,
                "listings": [],
                "match_found": False,
                "reply_message": WA_NO_MATCH_BUY_MESSAGE,
                "reply_phone_number": None,
                "matches": [],
            }
        listings = _apply_location_resolution(listings)
        inserted_ids: list[ObjectId] = []
        dupes = 0
        for listing in listings:
            inserted_id = insert_listing(listing)
            if inserted_id:
                listing["_id"] = inserted_id
                if listing.pop("_duplicate", False):
                    dupes += 1
                else:
                    inserted_ids.append(inserted_id)
            else:
                dupes += 1

        request_buy_ids = _request_listing_ids(listings, "buy")
        has_buy = any(
            (listing.get("transaction") or "").lower() == "buy"
            for listing in listings
            if isinstance(listing, dict)
        )

        combined_matches: list[dict] = []
        match_found = False
        if has_buy:
            matches = run_matching()
            project_matches = run_project_matching()
            match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])
            relevant_docs = [doc for doc in match_docs if doc.get("buy_id") in request_buy_ids]
            relevant_projects = [
                match for match in project_matches
                if match.get("buy_id") in request_buy_ids
            ]
            combined_matches = _format_broker_sell_matches(relevant_docs) + _format_project_matches(relevant_projects)
            combined_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)

            match_found = bool(combined_matches)
            reply_message = _build_reply_message("buy", combined_matches)
            reply_phone_number = _best_broker_phone(combined_matches)
        else:
            sell_matches: list[dict] = []
            for listing in listings:
                if listing.get("transaction") != "sell":
                    continue
                sell_doc = listing if isinstance(listing.get("_id"), ObjectId) else _find_existing_listing_by_fingerprint(listing) or listing
                sell_matches.extend(run_matching_for_sell(sell_doc))

            sell_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)
            combined_matches = sell_matches
            match_found = bool(sell_matches)
            reply_message = _build_reply_message("sell", sell_matches)
            reply_phone_number = None

        return {
            "filtered": False,
            WA_FIELD_MESSAGE_ID: None,
            WA_FIELD_PHONE_NUMBER: None,
            WA_TIMESTAMP_FIELD: None,
            "inserted": len(inserted_ids),
            "duplicates_skipped": dupes,
            "listings": listings,
            "match_found": match_found,
            "reply_message": reply_message,
            "reply_phone_number": reply_phone_number,
            "matches": combined_matches,
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/ingest/whatsapp")
async def ingest_whatsapp(
    message_id: str = Form(...),
    phone_number: str = Form(...),
    raw_message: str | None = Form(None),
    file: UploadFile | None = File(None),
):
    received_at = datetime.utcnow().isoformat() + "Z"

    try:
        raw_message_text = (raw_message or "").strip()
        has_message = bool(raw_message_text)
        has_file = file is not None

        if not has_message and not has_file:
            return JSONResponse(status_code=400, content={"detail": "raw_message or file is required"})

        listings: list[dict] = []
        parsed_from_image = False
        content = None

        if has_file:
            content = await file.read()
            if content:
                encoded = base64.b64encode(content).decode("utf-8")
                mime_type = (file.content_type or "").strip() or "image/jpeg"
                if has_message:
                    listings = parse_image(
                        data_b64=encoded,
                        mime_type=mime_type,
                        context_text=raw_message_text,
                    )
                else:
                    listings = parse_image(data_b64=encoded, mime_type=mime_type)
                parsed_from_image = True
            elif not has_message:
                return JSONResponse(status_code=400, content={"detail": "file is required"})

        if not parsed_from_image and has_message:
            listings = parse_text_message(raw_message_text)

        if not listings:
            return {
                WA_FIELD_MESSAGE_ID: message_id,
                WA_FIELD_PHONE_NUMBER: phone_number,
                WA_TIMESTAMP_FIELD: received_at,
                "sent_by": None,
                "inserted": 0,
                "duplicates_skipped": 0,
                "listings_parsed": 0,
                "match_found": False,
                "reply_message": WA_NO_MATCH_BUY_MESSAGE,
                "reply_phone_number": None,
                "matches": [],
            }

        sent_by = None
        first_listing = listings[0] if listings else None
        if isinstance(first_listing, dict):
            sent_by = first_listing.get("sent_by")

        listings = _apply_location_resolution(listings)

        for listing in listings:
            if not isinstance(listing, dict):
                continue
            listing[WA_STORED_MESSAGE_ID] = message_id
            listing[WA_STORED_PHONE_NUMBER] = phone_number
            listing[WA_STORED_RECEIVED_AT] = received_at
            listing["wa_sent_by"] = sent_by

        inserted_ids: list[ObjectId] = []
        dupes = 0
        for listing in listings:
            inserted_id = insert_listing(listing)
            if inserted_id:
                listing["_id"] = inserted_id
                if listing.pop("_duplicate", False):
                    dupes += 1
                else:
                    inserted_ids.append(inserted_id)
            else:
                dupes += 1

        transactions = {
            listing.get("transaction")
            for listing in listings
            if isinstance(listing, dict)
        }
        has_buy = "buy" in transactions

        combined_matches: list[dict] = []
        match_found = False
        if has_buy:
            request_buy_ids = _request_listing_ids(listings, "buy")
            matches = run_matching()
            project_matches = run_project_matching()
            match_docs = _collect_match_docs([m["match_id"] for m in matches if m.get("match_id")])
            relevant_docs = [doc for doc in match_docs if doc.get("buy_id") in request_buy_ids]
            relevant_projects = [
                match for match in project_matches
                if match.get("buy_id") in request_buy_ids
            ]
            combined_matches = _format_broker_sell_matches(relevant_docs) + _format_project_matches(relevant_projects)
            combined_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)

            match_found = bool(combined_matches)
            reply_message = _build_reply_message("buy", combined_matches)
            reply_phone_number = _best_broker_phone(combined_matches)
        else:
            sell_matches: list[dict] = []
            for listing in listings:
                if listing.get("transaction") != "sell":
                    continue
                sell_doc = listing if isinstance(listing.get("_id"), ObjectId) else _find_existing_listing_by_fingerprint(listing) or listing
                sell_matches.extend(run_matching_for_sell(sell_doc))

            sell_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)
            combined_matches = sell_matches
            match_found = bool(sell_matches)
            reply_message = _build_reply_message("sell", sell_matches)
            reply_phone_number = phone_number

        return {
            WA_FIELD_MESSAGE_ID: message_id,
            WA_FIELD_PHONE_NUMBER: phone_number,
            WA_TIMESTAMP_FIELD: received_at,
            "sent_by": sent_by,
            "inserted": len(inserted_ids),
            "duplicates_skipped": dupes,
            "listings_parsed": len(listings),
            "match_found": match_found,
            "reply_message": reply_message,
            "reply_phone_number": reply_phone_number,
            "matches": combined_matches,
        }

    except Exception as e:
        print(f"[API] WhatsApp ingest failed: {e}")
        return {
            WA_FIELD_MESSAGE_ID: message_id,
            WA_FIELD_PHONE_NUMBER: phone_number,
            WA_TIMESTAMP_FIELD: received_at,
            "sent_by": None,
            "inserted": 0,
            "duplicates_skipped": 0,
            "listings_parsed": 0,
            "match_found": False,
            "reply_message": WA_NO_MATCH_BUY_MESSAGE,
            "reply_phone_number": None,
            "matches": [],
        }


@app.get("/stats")
async def get_stats():
    return count_listings()


@app.get("/matches")
async def get_matches(unnotified_only: bool = False):
    matches = get_all_matches()
    return _serialize(matches)
