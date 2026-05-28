"""
project_importer.py

Imports projects from an Excel master sheet into the MongoDB projects collection.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.config import PROJECTS_MASTER_EXCEL
from core.database import insert_project

EXPECTED_COLUMNS = [
    "Youtube",
    "ProjectName",
    "Developer",
    "AreaName",
    "PropertyType",
    "StartingPrice",
    "ImageLink",
    "Lat,Lng",
    "PDF",
    "RedSticker",
    "LifeStyle",
    "Handover",
    "Payment Plan",
    "Bedrooms",
]


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.upper() == "N/A":
            return None
        return stripped
    return value


def _parse_property_types(value: Any) -> list[str]:
    value = _normalize_value(value)
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    else:
        parts = [str(value).strip()]

    mapping = {
        "apartments": "apartment",
        "apartment": "apartment",
        "villas": "villa",
        "villa": "villa",
        "townhouses": "townhouse",
        "townhouse": "townhouse",
        "penthouses": "penthouse",
        "penthouse": "penthouse",
        "plots": "plot",
        "plot": "plot",
        "offices": "office",
        "office": "office",
        "studios": "studio",
        "studio": "studio",
        "warehouses": "warehouse",
        "warehouse": "warehouse",
    }

    normalized: list[str] = []
    for part in parts:
        key = part.lower()
        normalized.append(mapping.get(key, key))

    return sorted(set(normalized))


def _parse_bedrooms(value: Any) -> list[int]:
    value = _normalize_value(value)
    if value is None:
        return []

    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    results: list[int] = []

    for part in parts:
        part_lower = part.lower()
        if "studio" in part_lower:
            results.append(0)
            continue

        match = re.search(r"(\d+)", part_lower)
        if match:
            results.append(int(match.group(1)))

    return sorted(set(results))


def _parse_starting_price(value: Any) -> float | None:
    value = _normalize_value(value)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    cleaned = re.sub(r"[^0-9\.]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_lat_lng(value: Any) -> dict | None:
    value = _normalize_value(value)
    if value is None:
        return None

    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 2:
            return None
        try:
            lat = float(parts[0])
            lng = float(parts[1])
        except ValueError:
            return None
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            lat = float(value[0])
            lng = float(value[1])
        except ValueError:
            return None
    else:
        return None

    return {
        "type": "Point",
        "coordinates": [lng, lat],
    }


def _build_project_doc(row: tuple, header_map: dict[str, int]) -> dict:
    def _get_col(name: str) -> Any:
        idx = header_map.get(name)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    youtube = _normalize_value(_get_col("Youtube"))
    project_name = _normalize_value(_get_col("ProjectName"))
    developer = _normalize_value(_get_col("Developer"))
    area_name = _normalize_value(_get_col("AreaName"))
    property_type_raw = _normalize_value(_get_col("PropertyType"))
    starting_price_raw = _normalize_value(_get_col("StartingPrice"))
    image_link = _normalize_value(_get_col("ImageLink"))
    lat_lng_raw = _normalize_value(_get_col("Lat,Lng"))
    pdf_link = _normalize_value(_get_col("PDF"))
    red_sticker = _normalize_value(_get_col("RedSticker"))
    lifestyle = _normalize_value(_get_col("LifeStyle"))
    handover = _normalize_value(_get_col("Handover"))
    payment_plan = _normalize_value(_get_col("Payment Plan"))
    bedrooms_raw = _normalize_value(_get_col("Bedrooms"))

    property_types = _parse_property_types(property_type_raw)
    bhk_options = _parse_bedrooms(bedrooms_raw)
    starting_price = _parse_starting_price(starting_price_raw)
    location_coords = _parse_lat_lng(lat_lng_raw)

    return {
        "Youtube": youtube,
        "ProjectName": project_name,
        "Developer": developer,
        "AreaName": area_name,
        "PropertyType": property_type_raw,
        "StartingPrice": starting_price_raw,
        "ImageLink": image_link,
        "Lat,Lng": lat_lng_raw,
        "PDF": pdf_link,
        "RedSticker": red_sticker,
        "LifeStyle": lifestyle,
        "Handover": handover,
        "PaymentPlan": payment_plan,
        "Bedrooms": bedrooms_raw,
        "property_types": property_types,
        "bhk_options": bhk_options,
        "starting_price": starting_price,
        "location_coords": location_coords,
    }


def run_import(start_row: int = 3, excel_path: str | None = None) -> dict:
    path = Path(excel_path or PROJECTS_MASTER_EXCEL)

    workbook = load_workbook(filename=path, data_only=True) #, read_only=True
    sheet = workbook["Sheet 1"]

    header = [str(c.value).strip() if c.value is not None else "" for c in sheet[2]]
    header_map = {name: idx for idx, name in enumerate(header) if name}

    inserted = 0
    skipped = 0

    for row in sheet.iter_rows(min_row=start_row, values_only=True):
        if not row or not any(cell is not None and str(cell).strip() for cell in row):
            continue
        project_doc = _build_project_doc(row, header_map)
        result = insert_project(project_doc)
        if result == "duplicate":
            skipped += 1
        else:
            inserted += 1

    print(f"[ProjectImporter] {inserted} inserted, {skipped} duplicates skipped")
    return {"inserted": inserted, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import projects from Excel")
    parser.add_argument("--start-row", type=int, default=3, help="Start row (1-based)")
    parser.add_argument("--excel-path", type=str, default=None, help="Path to projects_master.xlsx")
    args = parser.parse_args()

    run_import(start_row=args.start_row, excel_path=args.excel_path)


if __name__ == "__main__":
    main()
