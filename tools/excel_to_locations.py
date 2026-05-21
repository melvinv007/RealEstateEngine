"""
excel_to_locations.py

Convert data/raw_aliases.csv into data/locations.csv with deduped aliases.
Validates canonical names against cache/unique_names.json.
"""

import csv
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

INPUT_PATH = "data/raw_aliases.csv"
OUTPUT_PATH = "data/locations.csv"
NAMES_CACHE = "cache/unique_names.json"


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_known_set(cache_path: Path) -> set[str]:
    if not cache_path.exists():
        return set()

    with cache_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    known: set[str] = set()

    for city in data.get("cities", []):
        name = _normalize(city)
        if name:
            known.add(name.lower())

    for entry in data.get("communities", []):
        name = _normalize(entry.get("canonical"))
        if name:
            known.add(name.lower())

    for entry in data.get("subcommunities", []):
        name = _normalize(entry.get("canonical"))
        if name:
            known.add(name.lower())

    for entry in data.get("properties", []):
        name = _normalize(entry.get("canonical"))
        if name:
            known.add(name.lower())

    return known


def _parse_aliases(raw: str, canonical: str) -> list[str]:
    canonical_norm = canonical.lower()
    parts = [part.strip().lower() for part in (raw or "").split(",") if part.strip()]
    return [part for part in parts if part != canonical_norm]


def convert() -> int:
    input_path = Path(INPUT_PATH)
    if not input_path.exists():
        print(f"[Error] Input not found: {INPUT_PATH}")
        return 1

    known_names = _build_known_set(Path(NAMES_CACHE))

    entries: "OrderedDict[str, dict[str, str]]" = OrderedDict()
    alias_lists: dict[str, list[str]] = {}
    alias_sets: dict[str, set[str]] = {}
    level_counts: dict[str, int] = {}
    warned = 0

    with input_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            print("[Error] Input CSV has no header.")
            return 1

        for row in reader:
            canonical = _normalize(row.get("canonical_name"))
            level = _normalize(row.get("level"))
            parent = _normalize(row.get("parent"))

            if not canonical:
                continue

            canonical_key = canonical.lower()
            aliases = _parse_aliases(row.get("aliases", ""), canonical)

            if canonical_key not in entries:
                entries[canonical_key] = {
                    "canonical_name": canonical,
                    "level": level,
                    "parent": parent,
                }
                alias_lists[canonical_key] = []
                alias_sets[canonical_key] = set()

                if known_names and canonical_key not in known_names:
                    warned += 1
                    print(f"[Warning] canonical_name not in cache: {canonical}")

            for alias in aliases:
                if alias in alias_sets[canonical_key]:
                    continue
                alias_sets[canonical_key].add(alias)
                alias_lists[canonical_key].append(alias)

    output_path = Path(OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["canonical_name", "level", "parent", "aliases"])

        for canonical_key, entry in entries.items():
            aliases = ", ".join(alias_lists.get(canonical_key, []))
            writer.writerow([
                entry.get("canonical_name", ""),
                entry.get("level", ""),
                entry.get("parent", ""),
                aliases,
            ])

            level_value = entry.get("level", "") or "unknown"
            level_counts[level_value] = level_counts.get(level_value, 0) + 1

    total = len(entries)
    print(f"Total entries: {total}")
    for level in sorted(level_counts):
        print(f"  {level}: {level_counts[level]}")

    if known_names:
        print(f"Warnings: {warned} canonical names not found in cache")

    return 0


def main() -> int:
    try:
        return convert()
    except Exception as exc:
        print(f"[Error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
