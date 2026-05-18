"""
geocode_locations.py

Reads an Excel file with columns: City | Community | Subcommunity | Property
(Property may be empty). For each row, builds a canonical name and geocodes it
using Nominatim. Adds new entries to data/coordinates.csv — skips if already present.
Excel file is never modified.

Usage:
    python tools/geocode_locations.py <path_to_excel.xlsx>
"""

import csv
import os
import sys
import time
from pathlib import Path

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from openpyxl import load_workbook

# ── Configuration ──────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Excel column headers (case-insensitive)
COL_CITY          = "City"
COL_COMMUNITY     = "Community"
COL_SUBCOMMUNITY  = "Subcommunity"
COL_PROPERTY      = "Property"

# What becomes the canonical_name key in coordinates.csv
# Options: "community" | "subcommunity" | "property"
# - "community"    → Al Aweer, Zabeel, Reem
# - "subcommunity" → Al Aweer 1, Al Murooj Complex, Mira
# - "property"     → Desert Palm, Gardenia (falls back to subcommunity if empty)
CANONICAL_LEVEL = "subcommunity"

# Which columns to include in the geocode query (always joined with City)
# Options: "community" | "subcommunity" | "property"
# "subcommunity" → "Al Aweer 1, Al Aweer, Dubai, UAE"
# "property"     → "Desert Palm, Al Aweer 1, Al Aweer, Dubai, UAE" (if property exists)
GEOCODE_LEVEL = "subcommunity"

# Appended to every geocode query; set "" to disable
APPEND_SUFFIX = "UAE"

# Skip rows where property is empty (useful if you only want named buildings)
SKIP_IF_NO_PROPERTY = False

# Skip rows where subcommunity is empty
SKIP_IF_NO_SUBCOMMUNITY = True

# Deduplicate: if two rows produce the same canonical_name, geocode only the first
SKIP_DUPLICATE_CANONICALS = True

# data/coordinates.csv settings
COORDINATES_CSV_PATH = str(_PROJECT_ROOT / "data" / "coordinates.csv")
CSV_CANONICAL_COLUMN = "canonical_name"
CSV_LAT_COLUMN       = "lat"
CSV_LNG_COLUMN       = "lng"

# Nominatim settings
NOMINATIM_DELAY_SECONDS = 2.0
NOMINATIM_USER_AGENT    = "dubai_realestate_geocoder_v1"

# Misc
VERBOSE = True      # Print a status line for every row
DRY_RUN = False     # True = print results but write nothing

# ──────────────────────────────────────────────────────────────────────────────


_geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=10)
_last_call: float = 0.0


# ── Nominatim ─────────────────────────────────────────────────────────────────

def _rate_limit() -> None:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < NOMINATIM_DELAY_SECONDS:
        time.sleep(NOMINATIM_DELAY_SECONDS - elapsed)
    _last_call = time.time()


def _geocode(query: str) -> tuple[float, float] | None:
    _rate_limit()
    try:
        result = _geolocator.geocode(query)
        return (result.latitude, result.longitude) if result else None
    except GeocoderTimedOut:
        if VERBOSE:
            print(f"\n  [Geocoder] Timeout — retrying '{query}'...")
        time.sleep(NOMINATIM_DELAY_SECONDS * 2)
        try:
            result = _geolocator.geocode(query)
            return (result.latitude, result.longitude) if result else None
        except Exception:
            return None
    except (GeocoderServiceError, Exception) as e:
        if VERBOSE:
            print(f"\n  [Geocoder] Error for '{query}': {e}")
        return None


# ── Query + canonical builders ─────────────────────────────────────────────────

def _build_query(city: str, community: str, subcommunity: str, property_: str) -> str:
    """Build the geocode search string from available fields."""
    parts = []

    if GEOCODE_LEVEL == "property" and property_:
        parts.append(property_)

    if GEOCODE_LEVEL in ("property", "subcommunity") and subcommunity:
        parts.append(subcommunity)

    if community:
        parts.append(community)

    if city:
        parts.append(city)

    if APPEND_SUFFIX:
        parts.append(APPEND_SUFFIX)

    return ", ".join(parts)


def _build_canonical(community: str, subcommunity: str, property_: str) -> str | None:
    """Build the canonical_name that will be stored in coordinates.csv."""
    if CANONICAL_LEVEL == "property":
        return property_ if property_ else (subcommunity or community or None)
    elif CANONICAL_LEVEL == "subcommunity":
        return subcommunity if subcommunity else (community or None)
    elif CANONICAL_LEVEL == "community":
        return community or None
    return None


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _load_csv_canonicals(csv_path: str) -> set[str]:
    """Return set of lower-cased canonical names already in coordinates.csv."""
    existing: set[str] = set()
    if not os.path.exists(csv_path):
        return existing
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get(CSV_CANONICAL_COLUMN, "").strip().lower()
            if name:
                existing.add(name)
    return existing


def _append_to_csv(csv_path: str, canonical: str, lat: float, lng: float) -> None:
    """Append one row to coordinates.csv, writing the header if file is new."""
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([CSV_CANONICAL_COLUMN, CSV_LAT_COLUMN, CSV_LNG_COLUMN])
        writer.writerow([canonical, lat, lng])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_header(v: object) -> str:
    return "" if v is None else str(v).strip().lower()


def _cell_str(row: tuple, idx: int) -> str:
    """Safely extract a string cell value from a row tuple."""
    if idx is None or idx >= len(row):
        return ""
    v = row[idx]
    return str(v).strip() if v is not None else ""


# ── Main ───────────────────────────────────────────────────────────────────────

def geocode_excel(excel_path: str) -> int:
    if not os.path.exists(excel_path):
        print(f"[Error] File not found: {excel_path}")
        return 1

    wb = load_workbook(excel_path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    if not rows:
        print("[Error] Excel sheet is empty.")
        return 1

    # Resolve column indices from header row
    header_norm = [_normalize_header(cell) for cell in rows[0]]

    def _col(name: str) -> int | None:
        key = _normalize_header(name)
        return header_norm.index(key) if key in header_norm else None

    col_city         = _col(COL_CITY)
    col_community    = _col(COL_COMMUNITY)
    col_subcommunity = _col(COL_SUBCOMMUNITY)
    col_property     = _col(COL_PROPERTY)

    missing = [
        name for name, idx in [
            (COL_CITY, col_city),
            (COL_COMMUNITY, col_community),
            (COL_SUBCOMMUNITY, col_subcommunity),
        ] if idx is None
    ]
    if missing:
        print(f"[Error] Missing required columns: {missing}")
        print(f"  Found headers: {[c for c in rows[0] if c]}")
        return 1

    if col_property is None:
        print(f"[Warning] Column '{COL_PROPERTY}' not found — property treated as empty for all rows.")

    # Load what's already in coordinates.csv
    csv_existing = _load_csv_canonicals(COORDINATES_CSV_PATH)
    print(f"[Setup] {len(csv_existing)} entries already in {COORDINATES_CSV_PATH}")

    stats = {
        "already_in_csv":    0,
        "skipped_no_subcom": 0,
        "skipped_no_prop":   0,
        "skipped_duplicate": 0,
        "geocoded":          0,
        "failed":            0,
    }

    # Track canonicals seen in this run (to avoid geocoding same name twice)
    seen_this_run: set[str] = set()

    for row_idx, row in enumerate(rows[1:], start=2):
        city         = _cell_str(row, col_city)
        community    = _cell_str(row, col_community)
        subcommunity = _cell_str(row, col_subcommunity)
        property_    = _cell_str(row, col_property) if col_property is not None else ""

        # Skip filters
        if SKIP_IF_NO_SUBCOMMUNITY and not subcommunity:
            stats["skipped_no_subcom"] += 1
            if VERBOSE:
                print(f"  Row {row_idx:>4}: SKIP  (no subcommunity) — {community}")
            continue

        if SKIP_IF_NO_PROPERTY and not property_:
            stats["skipped_no_prop"] += 1
            if VERBOSE:
                print(f"  Row {row_idx:>4}: SKIP  (no property) — {subcommunity or community}")
            continue

        # Build canonical name
        canonical = _build_canonical(community, subcommunity, property_)
        if not canonical:
            if VERBOSE:
                print(f"  Row {row_idx:>4}: SKIP  (no canonical could be built)")
            continue

        canonical_lower = canonical.strip().lower()

        # Already in coordinates.csv
        if canonical_lower in csv_existing:
            stats["already_in_csv"] += 1
            if VERBOSE:
                print(f"  Row {row_idx:>4}: SKIP  (already in CSV) — {canonical}")
            continue

        # Already geocoded in this run (same canonical from a different row)
        if SKIP_DUPLICATE_CANONICALS and canonical_lower in seen_this_run:
            stats["skipped_duplicate"] += 1
            if VERBOSE:
                print(f"  Row {row_idx:>4}: SKIP  (duplicate in run) — {canonical}")
            continue

        seen_this_run.add(canonical_lower)

        # Geocode
        query = _build_query(city, community, subcommunity, property_)
        if VERBOSE:
            print(f"  Row {row_idx:>4}: QUERY '{query}' ...", end=" ", flush=True)

        coords = _geocode(query)

        if coords:
            lat, lng = coords
            if VERBOSE:
                print(f"→ ({lat:.5f}, {lng:.5f})")

            if not DRY_RUN:
                _append_to_csv(COORDINATES_CSV_PATH, canonical, lat, lng)
                csv_existing.add(canonical_lower)

            stats["geocoded"] += 1
        else:
            if VERBOSE:
                print("→ FAILED")
            stats["failed"] += 1

    print(
        f"\n[Done]"
        f"\n  Geocoded:            {stats['geocoded']}"
        f"\n  Failed:              {stats['failed']}"
        f"\n  Already in CSV:      {stats['already_in_csv']}"
        f"\n  Duplicate (run):     {stats['skipped_duplicate']}"
        f"\n  Skipped (no subcom): {stats['skipped_no_subcom']}"
        f"\n  Skipped (no prop):   {stats['skipped_no_prop']}"
    )
    if DRY_RUN:
        print("  [Dry Run] Nothing written.")

    return 0


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python geocode_locations.py <path_to_locations.xlsx>")
        sys.exit(1)
    sys.exit(geocode_excel(sys.argv[1]))


if __name__ == "__main__":
    main()