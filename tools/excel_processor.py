"""
excel_processor.py

Reads a location_master.xlsx file and extracts unique location names with
parent relationships, then writes a JSON summary to cache/unique_names.json.
"""

import json
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

EXCEL_PATH = "data/location_master.xlsx"
OUTPUT_PATH = "cache/unique_names.json"
TIER3_THRESHOLD = 3


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _key(value: Any) -> str:
    return _normalize(value).lower()


def _cell(row: tuple[Any, ...], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return _normalize(row[idx])


def _sort_key(*parts: str) -> tuple[str, ...]:
    return tuple((part or "").lower() for part in parts)


def process_excel(excel_path: str = EXCEL_PATH) -> dict[str, Any]:
    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)

    header = next(rows, None)
    if not header:
        raise ValueError("Excel sheet is empty.")

    header_map: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = _key(name)
        if key:
            header_map[key] = idx

    def _col(name: str) -> int:
        key = _key(name)
        if key not in header_map:
            raise ValueError(f"Missing required column: {name}")
        return header_map[key]

    col_city = _col("City")
    col_community = _col("Community")
    col_subcommunity = _col("Subcommunity")
    col_property = _col("Property")

    total_rows = 0

    city_names: dict[str, str] = {}
    community_names: dict[str, str] = {}
    subcommunity_names: dict[str, str] = {}
    property_names: dict[str, str] = {}

    community_entries: dict[tuple[str, str], dict[str, str]] = {}
    subcommunity_entries: dict[tuple[str, str, str], dict[str, str]] = {}
    property_entries: dict[tuple[str, str, str, str], dict[str, str]] = {}
    property_subcommunities: dict[str, set[str]] = {}

    for row in rows:
        total_rows += 1

        city_raw = _cell(row, col_city)
        community_raw = _cell(row, col_community)
        subcommunity_raw = _cell(row, col_subcommunity)
        property_raw = _cell(row, col_property)

        city_key = _key(city_raw)
        community_key = _key(community_raw)
        subcommunity_key = _key(subcommunity_raw)
        property_key = _key(property_raw)

        if city_raw and city_key not in city_names:
            city_names[city_key] = city_raw
        if community_raw and community_key not in community_names:
            community_names[community_key] = community_raw
        if subcommunity_raw and subcommunity_key not in subcommunity_names:
            subcommunity_names[subcommunity_key] = subcommunity_raw
        if property_raw and property_key not in property_names:
            property_names[property_key] = property_raw

        if community_raw:
            entry_key = (community_key, city_key)
            if entry_key not in community_entries:
                community_entries[entry_key] = {
                    "canonical": community_names.get(community_key, community_raw),
                    "parent_city": city_names.get(city_key, city_raw),
                }

        if subcommunity_raw:
            entry_key = (subcommunity_key, community_key, city_key)
            if entry_key not in subcommunity_entries:
                subcommunity_entries[entry_key] = {
                    "canonical": subcommunity_names.get(subcommunity_key, subcommunity_raw),
                    "parent_community": community_names.get(community_key, community_raw),
                    "parent_city": city_names.get(city_key, city_raw),
                }

        if property_raw:
            entry_key = (property_key, subcommunity_key, community_key, city_key)
            if entry_key not in property_entries:
                property_entries[entry_key] = {
                    "canonical": property_names.get(property_key, property_raw),
                    "parent_subcommunity": subcommunity_names.get(subcommunity_key, subcommunity_raw),
                    "parent_community": community_names.get(community_key, community_raw),
                    "parent_city": city_names.get(city_key, city_raw),
                }

            # Only count non-empty subcommunities toward tier3 detection.
            if subcommunity_key:
                property_subcommunities.setdefault(property_key, set()).add(subcommunity_key)

    cities = sorted(city_names.values(), key=str.lower)

    communities = sorted(
        community_entries.values(),
        key=lambda entry: _sort_key(entry.get("canonical", ""), entry.get("parent_city", "")),
    )

    subcommunities = sorted(
        subcommunity_entries.values(),
        key=lambda entry: _sort_key(
            entry.get("canonical", ""),
            entry.get("parent_community", ""),
            entry.get("parent_city", ""),
        ),
    )

    properties: list[dict[str, Any]] = []
    for (property_key, _, _, _), entry in property_entries.items():
        subcommunity_count = len(property_subcommunities.get(property_key, set()))
        properties.append({
            **entry,
            "tier3": subcommunity_count >= TIER3_THRESHOLD,
            "subcommunity_count": subcommunity_count,
        })

    properties.sort(
        key=lambda entry: _sort_key(
            entry.get("canonical", ""),
            entry.get("parent_subcommunity", ""),
            entry.get("parent_community", ""),
            entry.get("parent_city", ""),
        )
    )

    payload = {
        "cities": cities,
        "communities": communities,
        "subcommunities": subcommunities,
        "properties": properties,
    }

    output_path = Path(OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    tier3_count = sum(1 for entry in properties if entry.get("tier3"))

    print(f"Total rows in Excel: {total_rows}")
    print(f"Unique cities count: {len(cities)}")
    print(f"Unique communities count: {len(communities)}")
    print(f"Unique subcommunities count: {len(subcommunities)}")
    print(f"Unique properties count: {len(properties)}")
    print(f"Tier 3 properties count: {tier3_count}")

    return payload


def main() -> int:
    try:
        process_excel()
        return 0
    except Exception as exc:
        print(f"[Error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
