"""
logger.py
Centralised structured event logger → MongoDB system_logs collection.

Usage from any module:
    from core.logger import log_event, set_request_id, get_request_id

log_event(
    log_type   = "gemini_error",          # see LOG_TYPES below
    level      = "error",                 # debug | info | warning | error | critical
    source     = "gemini_client",         # module name
    message    = "Daily quota hit",       # human-readable summary
    details    = {"model": "...", ...},   # structured payload
)

LOG_TYPES:
    gemini_error            — Gemini/Groq/OpenRouter API failures
    parse_failure           — Gemini returned unparseable or empty output
    parse_warning           — Post-parse heuristic correction applied (e.g. transaction flip)
    location_unresolved     — Resolver failed all layers
    location_mismatch       — Hint hierarchy was wrong (community/subcommunity parent mismatch)
    gemini_arbitration      — Gemini was called for location disambiguation
    duplicate_detected      — Fingerprint matched existing listing
    match_recorded          — A match was written to matches collection
    filter_rejected         — Message filtered out as non-real-estate
    ingest_request          — Full summary of one WhatsApp/text/image ingest call
    pipeline_event          — Pipeline watcher ran (started / completed / failed)
    api_error               — Unhandled exception in any API endpoint

Architecture:
    - Maintains its own lazy MongoDB connection (separate from database.py)
      to avoid circular imports (database.py also imports from logger.py).
    - request_id is a contextvars.ContextVar — set once at request entry in api.py,
      automatically readable here and in every module called within that request,
      without passing it around.
    - TTL index: logs auto-expire after LOG_TTL_DAYS days.
    - Falls back to print() if MongoDB is not available.
"""

from __future__ import annotations

import contextvars
import traceback
from datetime import datetime, timezone
from typing import Any

from core.config import MONGO_URI, MONGO_DB_NAME, PRODUCTION_MODE

# ── Constants ──────────────────────────────────────────────────────────────────

COLLECTION_LOGS = "system_logs"
LOG_TTL_DAYS    = 30          # auto-expire after this many days
_TTL_SECONDS    = LOG_TTL_DAYS * 24 * 3600

# ── Request ID context variable ────────────────────────────────────────────────
# Set via set_request_id() at the top of each API endpoint handler.
# Readable via get_request_id() from any module in the same request call chain.

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="cli"
)


def set_request_id(rid: str) -> None:
    """Call this at the top of each FastAPI endpoint handler."""
    _request_id_var.set(rid)


def get_request_id() -> str:
    """Returns current request_id, or 'cli' when called outside a request."""
    return _request_id_var.get()


# ── Lazy MongoDB connection ────────────────────────────────────────────────────
# Separate from database.py to avoid circular imports.

_log_client = None
_log_db     = None
_log_coll   = None
_indexes_ok = False


def _get_log_collection():
    global _log_client, _log_db, _log_coll, _indexes_ok

    if _log_coll is not None:
        return _log_coll

    try:
        from pymongo import MongoClient, ASCENDING
        _log_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _log_db     = _log_client[MONGO_DB_NAME]
        _log_coll   = _log_db[COLLECTION_LOGS]

        if not _indexes_ok:
            _ensure_log_indexes(_log_coll)
            _indexes_ok = True

        return _log_coll

    except Exception as exc:
        print(f"[Logger] MongoDB unavailable — logs will be printed only: {exc}")
        return None


def _ensure_log_indexes(coll) -> None:
    """
    Create indexes on system_logs.
    Called once on first connection.
    Pass the collection object directly to avoid circular import with database.py.
    """
    from pymongo import ASCENDING

    # TTL index — auto-delete documents older than LOG_TTL_DAYS
    coll.create_index(
        [("timestamp", ASCENDING)],
        expireAfterSeconds=_TTL_SECONDS,
        name="ttl_30d",
        background=True,
    )
    # Query indexes for fast filtering by type and level
    coll.create_index([("log_type", ASCENDING)], name="log_type_idx", background=True)
    coll.create_index([("level", ASCENDING)],    name="level_idx",    background=True)
    coll.create_index([("source", ASCENDING)],   name="source_idx",   background=True)
    coll.create_index([("request_id", ASCENDING)], name="req_id_idx", background=True)
    # Compound index for most common query pattern: type + level + time
    coll.create_index(
        [("log_type", ASCENDING), ("level", ASCENDING), ("timestamp", ASCENDING)],
        name="type_level_time_idx",
        background=True,
    )


# ── Core logging function ──────────────────────────────────────────────────────

def log_event(
    log_type: str,
    level: str,
    source: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Write a structured log event to MongoDB system_logs.

    Args:
        log_type:  Event category (see LOG_TYPES in module docstring)
        level:     Severity — "debug" | "info" | "warning" | "error" | "critical"
        source:    Module that generated this event (e.g. "gemini_client", "parser")
        message:   Human-readable one-line summary
        details:   Structured payload dict — keys vary by log_type
    """
    level = (level or "info").lower()

    # In production, suppress debug events entirely
    if PRODUCTION_MODE and level == "debug":
        return

    now = datetime.now(timezone.utc)

    doc: dict[str, Any] = {
        "log_type":   log_type,
        "level":      level,
        "source":     source,
        "message":    message,
        "request_id": get_request_id(),
        "timestamp":  now,
        "timestamp_local": now.astimezone().isoformat(),  # local timezone for readability
        "details":    details or {},
    }

    # Terminal output — always print warnings/errors, print info only in dev
    _terminal_print(level, source, message, details)

    # MongoDB write
    try:
        coll = _get_log_collection()
        if coll is not None:
            coll.insert_one(doc)
    except Exception as exc:
        # Never let logging crash the application
        print(f"[Logger] Failed to write log to MongoDB: {exc}")


# ── Terminal formatting ────────────────────────────────────────────────────────

_LEVEL_PREFIX = {
    "debug":    "  ",
    "info":     "  ",
    "warning":  "⚠ ",
    "error":    "✗ ",
    "critical": "✗✗",
}


def _terminal_print(level: str, source: str, message: str, details: dict | None) -> None:
    """
    Print log event to terminal.
    Production: warnings and errors only.
    Dev: everything.
    """
    if PRODUCTION_MODE and level not in ("warning", "error", "critical"):
        return

    prefix = _LEVEL_PREFIX.get(level, "  ")
    rid    = get_request_id()
    tag    = f"[{rid}]" if rid != "cli" else "[cli]"

    print(f"{prefix}[{source.upper()}]{tag} {message}")

    # For errors, always print key detail fields to terminal for immediate visibility
    if level in ("error", "critical") and details:
        for key in ("error_type", "error", "model", "endpoint", "location_raw"):
            val = details.get(key)
            if val:
                print(f"   {key}: {val}")
        # Print traceback if present
        tb = details.get("traceback")
        if tb:
            for line in tb.strip().splitlines()[-6:]:   # last 6 lines only
                print(f"   {line}")


# ── Convenience helpers ────────────────────────────────────────────────────────

def log_gemini_error(
    model: str,
    provider: str,
    error_type: str,
    error_message: str,
    attempt: int,
    latency_s: float,
    prompt_snippet: str = "",
) -> None:
    """Shorthand for gemini_error log events."""
    log_event(
        "gemini_error", "error", "gemini_client",
        f"{provider}/{model} {error_type} (attempt {attempt})",
        {
            "model":          model,
            "provider":       provider,
            "error_type":     error_type,
            "error_message":  error_message[:500],
            "attempt":        attempt,
            "latency_s":      round(latency_s, 3),
            "prompt_snippet": prompt_snippet[:200],
        },
    )


def log_gemini_success(
    model: str,
    provider: str,
    latency_s: float,
) -> None:
    """Shorthand for successful Gemini call — debug level, suppressed in production."""
    log_event(
        "gemini_call", "debug", "gemini_client",
        f"{provider}/{model} OK ({latency_s:.2f}s)",
        {"model": model, "provider": provider, "latency_s": round(latency_s, 3)},
    )


def log_api_error(endpoint: str, error: Exception, extra: dict | None = None) -> None:
    """Shorthand for unhandled API exceptions."""
    details = {
        "endpoint":  endpoint,
        "error":     str(error),
        "traceback": traceback.format_exc(),
    }
    if extra:
        details.update(extra)
    log_event("api_error", "critical", "api", f"Unhandled exception in {endpoint}: {error}", details)