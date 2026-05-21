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
from pydantic import BaseModel

from core.config import API_KEY
from core.database import insert_many_listings, count_listings, get_all_matches
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


@app.get("/stats")
async def get_stats():
    return count_listings()


@app.get("/matches")
async def get_matches(unnotified_only: bool = False):
    matches = get_all_matches()
    return _serialize(matches)
