"""
main.py
CLI entry point for the Real Estate Matcher.

Usage:
    # Parse a text message file and run matching
    python main.py --text messages.txt

    # Parse an image flyer and run matching
    python main.py --image flyer.png

    # Pass raw text directly
    python main.py --raw "FOR SALE | Business Bay | 2BR | AED 2.5M"

    # Just run matching on existing DB data (no new input)
    python main.py --match-only

    # Print DB stats
    python main.py --stats

    # Print all matches
    python main.py --show-matches

    # ⚠️ Clear all data (testing only)
    python main.py --clear

    # To run as API server: uvicorn main:app --reload --port 8000
    # To expose publicly with ngrok: ngrok http 8000
"""

import argparse
import json
import os
import time
import re
from pathlib import Path
from bson import ObjectId
from datetime import datetime

from ingestion.parser import parse_input, parse_text_message
from location.resolver import resolve_location
from core.database import insert_many_listings, count_listings, get_all_matches, clear_all, dedupe_collection
from core.matcher import run_matching

# ── Rate limit config ──────────────────────────────────────────────────────────
# How many seconds to wait when a 429 is hit before retrying
_RATE_LIMIT_WAIT = 15
# Max retries per message before giving up and moving on
_RATE_LIMIT_MAX_RETRIES = 3
_MESSAGE_DELAY_SECONDS = 13  # Delay between processing messages to avoid hitting rate limits (e.g. geocoding)

_PROCESSED_CACHE_PATH = Path("data/processed_cache.txt")
_PARSE_ERROR_LOG = Path("cache/parse_errors.log")
_MESSAGE_SEPARATOR = "\n---\n"


def _serialize(obj):
    """JSON serializer for MongoDB ObjectId and datetime."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def print_json(data):
    print(json.dumps(data, indent=2, default=_serialize))


def print_match_report(matches: list[dict]):
    if not matches:
        print("\n✅ No new matches found.")
        return

    print(f"\n{'='*60}")
    print(f"  🎯 {len(matches)} MATCH(ES) FOUND")
    print(f"{'='*60}")

    for i, m in enumerate(matches, 1):
        print(f"\n── Match #{i} ──────────────────────────────────")
        print(f"  Match ID   : {m['match_id']}")
        print(f"  Score      : {m['score']:.2%}")
        print(f"  Buy  ID    : {m['buy_id']}")
        print(f"  Sell ID    : {m['sell_id']}")
        print(f"  Reasons    :")
        for r in m["reasons"]:
            print(f"    ✔ {r}")


def _split_messages(content: str) -> list[str]:
    # Common separators to try, in order of preference
    separators = [
        r'^\s*---+\s*$',
        r'^\s*===+\s*$',
        r'^\s*\*\*\*+\s*$',
        r'^\s*###\s*$',
        r'^\s*~~~+\s*$',
    ]
    for pattern in separators:
        parts = re.split(pattern, content, flags=re.MULTILINE)
        if len(parts) > 1:  # this separator worked
            messages = [p.strip() for p in parts if p.strip()]
            print(f"   Detected separator: '{pattern}'")
            return messages

    # Fallback — treat double blank line as separator
    parts = re.split(r'\n\s*\n\s*\n', content)
    messages = [p.strip() for p in parts if p.strip()]
    if len(messages) > 1:
        print(f"   Detected separator: blank lines")
        return messages

    # Last resort — whole file is one message
    return [content.strip()] if content.strip() else []


def _parse_with_retry(message: str, index: int, total: int) -> list[dict]:
    """
    Parse a single message with retry on rate limit (429).
    Returns listings list (may be empty if all retries fail).
    """
    for attempt in range(1, _RATE_LIMIT_MAX_RETRIES + 1):
        try:
            listings = parse_text_message(message)
            return listings
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                if attempt < _RATE_LIMIT_MAX_RETRIES:
                    print(
                        f"\n⏳ Rate limit hit on message {index}/{total} "
                        f"(attempt {attempt}/{_RATE_LIMIT_MAX_RETRIES}). "
                        f"Waiting {_RATE_LIMIT_WAIT}s..."
                    )
                    time.sleep(_RATE_LIMIT_WAIT)
                else:
                    print(
                        f"\n❌ Message {index}/{total} failed after "
                        f"{_RATE_LIMIT_MAX_RETRIES} retries due to rate limit. Skipping."
                    )
                    return []
            else:
                # Non-rate-limit error — don't retry
                print(f"\n❌ Message {index}/{total} failed: {e}")
                return []
    return []


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


def _append_processed_message(message: str) -> None:
    _PROCESSED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESSED_CACHE_PATH.open("a", encoding="utf-8") as file:
        file.write(message.rstrip())
        file.write(_MESSAGE_SEPARATOR)


def _write_remaining_messages(filepath: str, remaining: list[str]) -> None:
    path = Path(filepath)
    temp_path = Path(str(path) + ".tmp")
    content = _MESSAGE_SEPARATOR.join(remaining)
    if content:
        content = content + "\n"
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def _log_parse_error(index: int, total: int, message: str, error: str) -> None:
    _PARSE_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    preview = message.replace("\n", " ")[:120]
    error_text = error[:200]
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{index}/{total}\t{error_text}\t{preview}"
    with _PARSE_ERROR_LOG.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _process_text_file(filepath: str) -> None:
    """
    Read messages.txt, split by separator, then for each message:
      parse → insert → (run matching after all done)
    Progress is saved after every message so a mid-run crash
    doesn't lose already-processed entries.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    messages = _split_messages(content)
    total = len(messages)

    if total == 0:
        print("⚠️  No messages found in file.")
        return

    print(f"\n📂 Found {total} message(s) in file. Processing...\n")

    total_extracted = 0
    total_inserted = 0
    total_dupes = 0
    total_failed = 0

    remaining_messages = list(messages)
    index = 0

    while index < len(remaining_messages):
        total_current = len(remaining_messages)
        message = remaining_messages[index]
        preview = message[:60].replace("\n", " ")

        listings = _parse_with_retry(message, index + 1, total_current)

        if not listings:
            print(f"⚠️  Message {index + 1}/{total_current} — no listings extracted: {preview}...")
            _log_parse_error(index + 1, total_current, message, "no listings extracted")
            total_failed += 1
            index += 1
        else:
            try:
                listings = _apply_location_resolution(listings)
                total_extracted += len(listings)
                inserted_ids, dupes = insert_many_listings(listings)
                total_inserted += len(inserted_ids)
                total_dupes += dupes
            except Exception as e:
                print(f"⚠️  Message {index + 1}/{total_current} — location resolution failed: {e}")
                _log_parse_error(index + 1, total_current, message, f"resolution error: {e}")
                total_failed += 1
                index += 1
                continue

            _append_processed_message(message)
            remaining_messages.pop(index)
            _write_remaining_messages(filepath, remaining_messages)

        # Delay between messages to stay under rate limit — skip after last one
        if index < len(remaining_messages):
            time.sleep(_MESSAGE_DELAY_SECONDS)

    # ── Run matching once after all messages processed ─────────────────────────
    print(f"\n{'='*60}")
    print(f"  📊 PROCESSING COMPLETE")
    print(f"{'='*60}")
    print(f"  Messages processed : {total - total_failed}/{total}")
    print(f"  Listings extracted : {total_extracted}")
    print(f"  Inserted into DB   : {total_inserted}")
    print(f"  Duplicates skipped : {total_dupes}")
    print(f"  Failed/skipped     : {total_failed}")

    print(f"\n🔍 Running matching on all unmatched listings...")
    matches = run_matching()
    print_match_report(matches)

    stats = count_listings()
    print("\n📊 Updated DB Stats:")
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")

def main():
    parser = argparse.ArgumentParser(description="Real Estate WhatsApp Message Matcher")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", help="Path to a .txt file containing WhatsApp messages separated by ---")
    group.add_argument("--image", help="Path to an image/flyer file")
    group.add_argument("--raw", help="Raw message string to parse directly")
    group.add_argument("--match-only", action="store_true", help="Run matching without adding new data")
    group.add_argument("--stats", action="store_true", help="Print database statistics")
    group.add_argument("--show-matches", action="store_true", help="Print all recorded matches")
    group.add_argument("--clear", action="store_true", help="⚠️ Clear all data (irreversible)")
    group.add_argument("--dedupe", action="store_true", help="Remove duplicate listings from buy and sell collections")
    args = parser.parse_args()

    # ── Stats ──────────────────────────────────────────────────────────────────
    if args.stats:
        stats = count_listings()
        print("\n📊 Database Statistics:")
        for k, v in stats.items():
            print(f"  {k:20s}: {v}")
        return

    # ── Show Matches ───────────────────────────────────────────────────────────
    if args.show_matches:
        matches = get_all_matches()
        print(f"\n📋 All Matches ({len(matches)} total):")
        print_json(matches)
        return

    # ── Dedupe ─────────────────────────────────────────────────────────────────
    if args.dedupe:
        print("\n🧹 Running deduplication...")
        for txn in ("buy", "sell"):
            result = dedupe_collection(txn)
            print(f"  {txn:4s}: scanned {result['scanned']:4d} | removed {result['removed']:4d} duplicates")
        stats = count_listings()
        print("\n📊 Updated DB Stats:")
        for k, v in stats.items():
            print(f"  {k:20s}: {v}")
        return

    # ── Clear ──────────────────────────────────────────────────────────────────
    if args.clear:
        confirm = input("⚠️  This will delete ALL data. Type 'yes' to confirm: ")
        if confirm.strip().lower() == "yes":
            clear_all()
        else:
            print("Aborted.")
        return

    # ── Match Only ─────────────────────────────────────────────────────────────
    if args.match_only:
        matches = run_matching()
        print_match_report(matches)
        return

    # ── Text file — per-message processing ────────────────────────────────────
    if args.text:
        _process_text_file(args.text)
        return

    # ── Single image ───────────────────────────────────────────────────────────
    if args.image:
        listings = parse_input(args.image)
        if not listings:
            print("⚠️  No listings extracted from image.")
            return
        listings = _apply_location_resolution(listings)
        print(f"\n📥 Extracted {len(listings)} listing(s):")
        print_json(listings)
        inserted_ids, dupes = insert_many_listings(listings)
        print(f"\n✅ Inserted {len(inserted_ids)} listing(s). ({dupes} duplicate(s) skipped)")
        matches = run_matching()
        print_match_report(matches)
        stats = count_listings()
        print("\n📊 Updated DB Stats:")
        for k, v in stats.items():
            print(f"  {k:20s}: {v}")
        return

    # ── Raw string ─────────────────────────────────────────────────────────────
    if args.raw:
        listings = parse_text_message(args.raw)
        if not listings:
            print("⚠️  No listings extracted from input.")
            return
        listings = _apply_location_resolution(listings)
        print(f"\n📥 Extracted {len(listings)} listing(s):")
        print_json(listings)
        inserted_ids, dupes = insert_many_listings(listings)
        print(f"\n✅ Inserted {len(inserted_ids)} listing(s). ({dupes} duplicate(s) skipped)")
        matches = run_matching()
        print_match_report(matches)
        stats = count_listings()
        print("\n📊 Updated DB Stats:")
        for k, v in stats.items():
            print(f"  {k:20s}: {v}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()

from ingestion.api import app  # noqa — makes `uvicorn main:app` work