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
"""

import argparse
import json
from bson import ObjectId
from datetime import datetime

from parser import parse_input, parse_text_message
from database import insert_many_listings, count_listings, get_all_matches, clear_all, dedupe_collection
from matcher import run_matching


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


def main():
    parser = argparse.ArgumentParser(description="Real Estate WhatsApp Message Matcher")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text", help="Path to a .txt file containing WhatsApp messages")
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

    # ── Dedupe ────────────────────────────────────────────────────────────────
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

    # ── Parse Input ────────────────────────────────────────────────────────────
    listings = []

    if args.text:
        with open(args.text, "r", encoding="utf-8") as f:
            content = f.read()
        listings = parse_text_message(content)

    elif args.image:
        listings = parse_input(args.image)

    elif args.raw:
        listings = parse_text_message(args.raw)

    else:
        parser.print_help()
        return

    if not listings:
        print("⚠️  No listings extracted from input.")
        return

    print(f"\n📥 Extracted {len(listings)} listing(s):")
    print_json(listings)

    # ── Insert into DB ─────────────────────────────────────────────────────────
    inserted_ids = insert_many_listings(listings)
    print(f"\n✅ Inserted {len(inserted_ids)} listing(s) into MongoDB.")

    # ── Run Matching ───────────────────────────────────────────────────────────
    matches = run_matching()
    print_match_report(matches)

    # ── Print updated stats ────────────────────────────────────────────────────
    stats = count_listings()
    print("\n📊 Updated DB Stats:")
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")


if __name__ == "__main__":
    main()