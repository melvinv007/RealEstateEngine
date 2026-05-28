"""
matcher.py
Buy/sell matching engine with:
- Fixed price logic (strict bounded range)
- MongoDB pre-filtering for efficiency
- Minimum score gate
- Mandatory field requirements
- Historical match prevention
"""

from bson import ObjectId
from rapidfuzz import fuzz
from core.config import (
    PRICE_TOLERANCE,
    DISTANCE_KM_TOLERANCE,
    BHK_TOLERANCE,
    SQFT_TOLERANCE,
    DELETE_AFTER_MATCH,
    MIN_MATCH_SCORE,
    REQUIRE_PRICE_OR_LOCATION,
    COLLECTION_BUY,
    COLLECTION_SELL,
    COLLECTION_PROJECTS,
    FUZZY_LOCATION_THRESHOLD,
)
from core.database import (
    get_all_active,
    get_active_filtered,
    record_match,
    already_matched_pair,
    get_db,
    get_all_projects,
    record_project_match,
    already_matched_project_pair,
)


# ── Individual field checks ────────────────────────────────────────────────────

def _price_check(buy: dict, sell: dict) -> tuple[bool, bool, str]:
    """
    Returns (passed, was_skipped, reason).

    FIXED LOGIC:
    - Buyer budget is treated as a ceiling (or a range if min/max given).
    - Sell price must fall within [budget * (1 - TOL), budget * (1 + TOL)].
    - If buyer only gives one price, it's treated as MAX budget.
      e.g. buyer says "max 4.5M" → allowed range: 4.05M – 4.95M
    - If buyer gives range (4M–5M) → allowed: 3.6M – 5.5M
    - NEVER match sell price > buyer_max * (1 + PRICE_TOLERANCE).
    """
    sell_price = sell.get("price_aed")
    if sell_price is None:
        return True, True, "price_skipped(no_sell_price)"

    buy_min = buy.get("price_min_aed")
    buy_max = buy.get("price_max_aed")
    buy_single = buy.get("price_aed")

    # Determine effective buyer range
    if buy_min is not None and buy_max is not None:
        # Buyer gave explicit range
        effective_min = buy_min * (1 - PRICE_TOLERANCE)
        effective_max = buy_max * (1 + PRICE_TOLERANCE)
    elif buy_single is not None:
        # Single budget = treat as max budget
        effective_min = buy_single * (1 - PRICE_TOLERANCE)
        effective_max = buy_single * (1 + PRICE_TOLERANCE)
    else:
        return True, True, "price_skipped(no_buy_budget)"

    if effective_min <= sell_price <= effective_max:
        return True, False, f"price_match(sell={sell_price:,.0f} in [{effective_min:,.0f}–{effective_max:,.0f}])"

    return False, False, f"price_mismatch(sell={sell_price:,.0f} outside [{effective_min:,.0f}–{effective_max:,.0f}])"


def _property_type_check(buy: dict, sell: dict) -> tuple[bool, bool, str]:
    b = (buy.get("property_type") or "").lower().strip()
    s = (sell.get("property_type") or "").lower().strip()
    if not b or not s:
        return True, True, "type_skipped"
    if b == s:
        return True, False, f"type_match({b})"
    return False, False, f"type_mismatch({b} vs {s})"


def _bhk_check(buy: dict, sell: dict) -> tuple[bool, bool, str]:
    b = buy.get("bhk")
    s = sell.get("bhk")
    if b is None or s is None:
        return True, True, "bhk_skipped"
    if abs(b - s) <= BHK_TOLERANCE:
        return True, False, f"bhk_match({b}BR)"
    return False, False, f"bhk_mismatch({b}BR vs {s}BR)"


def _sqft_check(buy: dict, sell: dict) -> tuple[bool, bool, str]:
    if SQFT_TOLERANCE is None:
        return True, True, "sqft_skipped(disabled)"
    b = buy.get("sqft")
    s = sell.get("sqft")
    if b is None or s is None:
        return True, True, "sqft_skipped"
    ratio = abs(b - s) / max(b, s)
    if ratio <= SQFT_TOLERANCE:
        return True, False, f"sqft_match({b} vs {s}, diff={ratio:.1%})"
    return False, False, f"sqft_mismatch({b} vs {s}, diff={ratio:.1%})"


def _location_check(buy: dict, sell: dict) -> tuple[bool, bool, str]:
    if buy.get("location_unresolved") or not buy.get("location_coords"):
        return True, True, "location_skipped(no_coords)"
    if sell.get("location_unresolved"):
        return True, True, "location_skipped(unresolved)"

    dist = sell.get("_distance_km")
    if dist is None:
        return True, True, "location_skipped(no_distance)"

    b_loc = (buy.get("location") or "").strip()
    s_loc = (sell.get("location") or "").strip()
    if b_loc and s_loc and b_loc.lower() == s_loc.lower():
        return True, False, f"location_exact_match({b_loc})"

    if dist <= DISTANCE_KM_TOLERANCE:
        return True, False, f"location_match(dist={dist:.2f}km ≤ {DISTANCE_KM_TOLERANCE}km)"

    return False, False, f"location_mismatch(dist={dist:.2f}km > {DISTANCE_KM_TOLERANCE}km)"


def _bhk_bounds(bhk: int | None) -> tuple[int | None, int | None]:
    if bhk is None:
        return None, None
    min_bhk = bhk - BHK_TOLERANCE
    max_bhk = bhk + BHK_TOLERANCE
    if min_bhk < 0:
        min_bhk = 0
    return min_bhk, max_bhk


def _has_valid_coords(listing: dict) -> bool:
    coords = listing.get("location_coords")
    if not isinstance(coords, dict):
        return False
    coord_list = coords.get("coordinates")
    return isinstance(coord_list, (list, tuple)) and len(coord_list) == 2


def _geo_candidates_for_buy(buy: dict) -> list[dict]:
    coords = buy.get("location_coords")
    if not isinstance(coords, dict):
        return []

    coord_list = coords.get("coordinates")
    if not isinstance(coord_list, (list, tuple)) or len(coord_list) != 2:
        return []

    lng, lat = coord_list

    query: dict = {}
    if buy.get("property_type"):
        query["property_type"] = buy.get("property_type")

    bhk_min, bhk_max = _bhk_bounds(buy.get("bhk"))
    if bhk_min is not None and bhk_max is not None:
        query["bhk"] = {"$gte": bhk_min, "$lte": bhk_max}

    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [lng, lat]},
                "distanceField": "distance_m",
                "maxDistance": DISTANCE_KM_TOLERANCE * 1000,
                "query": query,
                "spherical": True,
            }
        }
    ]

    db = get_db()
    return list(db[COLLECTION_SELL].aggregate(pipeline))


def _geo_candidates_buys_for_sell(sell: dict) -> list[dict]:
    coords = sell.get("location_coords")
    if not isinstance(coords, dict):
        return []

    coord_list = coords.get("coordinates")
    if not isinstance(coord_list, (list, tuple)) or len(coord_list) != 2:
        return []

    lng, lat = coord_list

    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [lng, lat]},
                "distanceField": "distance_m",
                "maxDistance": DISTANCE_KM_TOLERANCE * 1000,
                "spherical": True,
            }
        }
    ]

    db = get_db()
    buys = list(db[COLLECTION_BUY].aggregate(pipeline))
    for buy in buys:
        distance_m = buy.get("distance_m")
        if distance_m is not None:
            buy["_distance_km"] = distance_m / 1000.0
    return buys


# ── Scoring ────────────────────────────────────────────────────────────────────

FIELD_WEIGHTS = {
    "price_match":           0.35,
    "type_match":            0.25,
    "location_exact_match":  0.20,
    "location_match":        0.15,
    "location_fuzzy_match":  0.15,
    "bhk_match":             0.15,
    "sqft_match":            0.10,
}


def _compute_score(reasons: list[str]) -> float:
    score = 0.0
    for reason in reasons:
        for key, weight in FIELD_WEIGHTS.items():
            if reason.startswith(key):
                score += weight
                break
    return min(score, 1.0)


# ── Core match function ────────────────────────────────────────────────────────

def match_single(buy: dict, sell: dict) -> dict | None:
    """
    Attempt to match one buy against one sell.
    Returns match info dict on success, None on failure.
    """
    checks = [
        _property_type_check(buy, sell),
        _bhk_check(buy, sell),
        _price_check(buy, sell),
        _sqft_check(buy, sell),
        _location_check(buy, sell),
    ]

    passed_reasons = []
    skipped_fields = []

    for passed, skipped, reason in checks:
        if not passed:
            return None  # Hard failure — immediately disqualify
        if skipped:
            skipped_fields.append(reason)
        else:
            passed_reasons.append(reason)

    # Quality gate 1: require price OR location to have actually matched (not just skipped)
    if REQUIRE_PRICE_OR_LOCATION:
        has_price = any(r.startswith("price_match") for r in passed_reasons)
        has_location = any(r.startswith("location") for r in passed_reasons)
        if not has_price and not has_location:
            return None  # Both skipped — too weak

    score = _compute_score(passed_reasons)

    # Quality gate 2: minimum score threshold
    if score < MIN_MATCH_SCORE:
        return None

    return {
        "buy_id": buy["_id"],
        "sell_id": sell["_id"],
        "score": score,
        "reasons": passed_reasons,
        "skipped": skipped_fields,
    }


# ── Main matching runner ───────────────────────────────────────────────────────

def run_matching() -> list[dict]:
    """
    Efficient matching pipeline:
    1. For each unmatched buy listing, pre-filter sells by type + BHK using MongoDB indexes
    2. Run Python-side price + location checks only on filtered candidates
    3. Pick best scoring sell for each buy (1-to-1 matching)
    4. Enforce historical match prevention
    """
    buy_listings = get_all_active("buy")

    if not buy_listings:
        print("[Matcher] No unmatched buy listings.")
        return []

    matched_sell_ids: set[ObjectId] = set()
    results = []

    print(f"[Matcher] Processing {len(buy_listings)} buy listing(s)...")

    for buy in buy_listings:
        buy_id = buy["_id"]

        use_geo = _has_valid_coords(buy) and not buy.get("location_unresolved")

        if use_geo:
            sell_candidates = _geo_candidates_for_buy(buy)
            for sell in sell_candidates:
                distance_m = sell.get("distance_m")
                if distance_m is not None:
                    sell["_distance_km"] = distance_m / 1000.0
        else:
            # ── Pre-filter sell candidates using MongoDB indexes ──────────────
            # Only fetch sells that match property_type and BHK — skips unrelated listings entirely
            sell_candidates = get_active_filtered(
                transaction="sell",
                property_type=buy.get("property_type"),
                bhk=buy.get("bhk") if BHK_TOLERANCE == 0 else None,  # skip BHK filter if tolerance > 0
            )

        if not sell_candidates:
            continue

        valid_matches: list[dict] = []

        for sell in sell_candidates:
            sell_id = sell["_id"]
            if sell_id in matched_sell_ids:
                continue

            # Historical match prevention
            if already_matched_pair(buy_id, sell_id):
                continue

            m = match_single(buy, sell)
            if m:
                valid_matches.append(m)

        valid_matches.sort(key=lambda match: match["score"], reverse=True)

        for match in valid_matches:
            match_id = record_match(
                buy_id=match["buy_id"],
                sell_id=match["sell_id"],
                score=match["score"],
                reasons=match["reasons"],
                delete_after=DELETE_AFTER_MATCH,
            )
            if match_id:
                matched_sell_ids.add(match["sell_id"])
                results.append({**match, "match_id": match_id})

    print(f"[Matcher] Done. {len(results)} match(es) recorded.")
    return results


def run_matching_for_sell(sell: dict) -> list[dict]:
    if not sell or (sell.get("transaction") or "").lower() != "sell":
        return []

    sell_id = sell.get("_id")
    if sell_id is None:
        return []

    use_geo = _has_valid_coords(sell) and not sell.get("location_unresolved")

    if use_geo:
        buy_candidates = _geo_candidates_buys_for_sell(sell)
    else:
        buy_candidates = get_active_filtered(
            transaction="buy",
            property_type=sell.get("property_type"),
            bhk=sell.get("bhk") if BHK_TOLERANCE == 0 else None,
        )

    if not buy_candidates:
        return []

    best_matches: list[dict] = []

    for buy in buy_candidates:
        buy_id = buy.get("_id")
        if buy_id is None:
            continue

        if already_matched_pair(buy_id, sell_id):
            continue

        if use_geo:
            sell_copy = {**sell, "_distance_km": buy.get("_distance_km")}
        else:
            sell_copy = sell

        m = match_single(buy, sell_copy)
        if not m:
            continue

        best_matches.append({
            "match_type": "broker_buy",
            "buy_id": buy_id,
            "sell_id": sell_id,
            "score": m["score"],
            "reasons": m["reasons"],
            "skipped": m["skipped"],
            "buy_broker": buy.get("broker", {}),
            "buy_snapshot": {
                "property_type": buy.get("property_type"),
                "bhk": buy.get("bhk"),
                "price_aed": buy.get("price_aed"),
                "price_min_aed": buy.get("price_min_aed"),
                "price_max_aed": buy.get("price_max_aed"),
                "location": buy.get("location"),
                "sqft": buy.get("sqft"),
            },
        })

        record_match(
            buy_id=buy_id,
            sell_id=sell_id,
            score=m["score"],
            reasons=m["reasons"],
            delete_after=DELETE_AFTER_MATCH,
        )

    best_matches.sort(key=lambda m: m.get("score") or 0.0, reverse=True)
    return best_matches


def _geo_candidates_for_projects(buy: dict) -> list[dict]:
    coords = buy.get("location_coords")
    if not isinstance(coords, dict):
        return []

    coord_list = coords.get("coordinates")
    if not isinstance(coord_list, (list, tuple)) or len(coord_list) != 2:
        return []

    lng, lat = coord_list

    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [lng, lat]},
                "distanceField": "distance_m",
                "maxDistance": DISTANCE_KM_TOLERANCE * 1000,
                "spherical": True,
            }
        }
    ]

    db = get_db()
    return list(db[COLLECTION_PROJECTS].aggregate(pipeline))


def run_project_matching() -> list[dict]:
    results = []

    buy_listings = get_all_active("buy")
    if not buy_listings:
        return results

    for buy in buy_listings:
        if (buy.get("transaction") or "").lower() != "buy":
            continue

        use_geo = _has_valid_coords(buy) and not buy.get("location_unresolved")
        if use_geo:
            candidates = _geo_candidates_for_projects(buy)
            for project in candidates:
                distance_m = project.get("distance_m")
                if distance_m is not None:
                    project["_distance_km"] = distance_m / 1000.0
        else:
            candidates = get_all_projects()

        if not candidates:
            continue

        for project in candidates:
            reasons: list[str] = []
            skipped: list[str] = []

            project_types = project.get("property_types") or []
            project_bhks = project.get("bhk_options") or []

            buy_type = (buy.get("property_type") or "").lower().strip()
            if buy_type:
                if buy_type not in project_types:
                    continue
                reasons.append(f"type_match({buy_type})")
            else:
                skipped.append("type_skipped")

            buy_bhk = buy.get("bhk")
            if buy_bhk is not None:
                if buy_bhk not in project_bhks:
                    continue
                reasons.append(f"bhk_match({buy_bhk}BR)")
            else:
                skipped.append("bhk_skipped")

            buyer_max = buy.get("price_max_aed")
            if buyer_max is None:
                buyer_max = buy.get("price_aed")
            if buyer_max is None:
                buyer_max = buy.get("price_min_aed")

            project_price = project.get("starting_price")
            if buyer_max is None:
                skipped.append("price_skipped")
            elif project_price is None:
                skipped.append("price_skipped(no_project_price)")
            elif buyer_max >= project_price:
                reasons.append(
                    f"price_match(budget={buyer_max} >= starting={project_price})"
                )
            else:
                continue

            if use_geo and project.get("_distance_km") is not None:
                dist = project.get("_distance_km")
                if dist <= DISTANCE_KM_TOLERANCE:
                    reasons.append(f"location_match(dist={dist:.2f}km)")
                else:
                    continue
            else:
                buy_loc = buy.get("location_raw") or ""
                area = project.get("AreaName") or ""
                if buy_loc and area:
                    score = fuzz.WRatio(buy_loc, area)
                    if score >= FUZZY_LOCATION_THRESHOLD:
                        reasons.append(f"location_fuzzy_match(area={area}, score={score})")
                    else:
                        skipped.append("location_skipped")
                else:
                    skipped.append("location_skipped")

            if REQUIRE_PRICE_OR_LOCATION:
                has_price = any(r.startswith("price_match") for r in reasons)
                has_location = any(
                    r.startswith("location_match") or r.startswith("location_fuzzy_match")
                    for r in reasons
                )
                if not has_price and not has_location:
                    continue

            score = _compute_score(reasons)
            if score < MIN_MATCH_SCORE:
                continue

            buy_id = buy.get("_id")
            project_id = project.get("_id")
            if buy_id is None or project_id is None:
                continue

            if already_matched_project_pair(buy_id, project_id):
                continue

            record_project_match(
                buy_id=buy_id,
                project_id=project_id,
                score=score,
                reasons=reasons,
                buy_snapshot=buy,
                project_snapshot=project,
            )

            results.append({
                "match_type": "project",
                "buy_id": buy_id,
                "project_id": project_id,
                "score": score,
                "reasons": reasons,
                "skipped": skipped,
                "project_name": project.get("ProjectName"),
                "developer": project.get("Developer"),
                "area": project.get("AreaName"),
                "starting_price": project.get("starting_price"),
                "handover": project.get("Handover"),
                "payment_plan": project.get("PaymentPlan"),
                "bedrooms_available": project.get("bhk_options"),
                "youtube_link": project.get("Youtube"),
                "image_link": project.get("ImageLink"),
                "pdf_link": project.get("PDF"),
            })

    return results