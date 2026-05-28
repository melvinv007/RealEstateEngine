"""
location_resolver.py
Resolve raw Dubai location strings to canonical names.

Pipeline:
  L0 — Cache (exact norm key hit)
  L1 — Exact alias match
  L2 — Candidate generation + confidence scoring
         • Split input into {base, phase}
         • Fuzzy match base against all alias bases → top-N candidates
         • Score each by: fuzzy similarity + token coverage + phase agreement
         • Decision:
             HIGH confidence + clear winner  → return directly
             AMBIGUOUS (top candidates close) → Gemini disambiguate
             LOW confidence                  → Gemini cold
  L3 — Gemini (3 modes: disambiguate / confirm / cold)
  L4 — Unresolved logger + cache as UNKNOWN
"""

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.gemini_client import call_gemini
from rapidfuzz import fuzz, process

from core.config import GEMINI_API_KEY, PRODUCTION_MODE
from location.location_tree import build_or_load_tree, normalize_key, split_phase

MIN_CONFIDENCE = 0.65
HIGH_CONFIDENCE = 0.82
AMBIGUITY_BAND = 0.08
USE_EMBEDDINGS = False
W_FUZZY = 0.50
W_TOKEN_COVERAGE = 0.35
W_PHASE = 0.15

LOCATION_CACHE = "cache/location_cache.json"
HINT_MISMATCH_LOG = "cache/hint_mismatches.log"
GEMINI_ARBITRATION_LOG = "cache/gemini_arbitrations.log"
UNRESOLVED_LOG = "cache/unresolved.log"


_LEVEL_ORDER = ["property", "subcommunity", "community", "city"]
_LEVEL_RANK = {"city": 1, "community": 2, "subcommunity": 3, "property": 4}
_LEVEL_SET_KEYS = {
    "city": "cities",
    "community": "communities",
    "subcommunity": "subcommunities",
    "property": "properties",
}

_CACHE: dict[str, dict[str, Any]] | None = None
_ALIAS_KEYS_BY_LEVEL: dict[str, set[str]] | None = None


@dataclass
class Candidate:
    canonical: str
    level: str
    confidence: float
    matched_via: str
    node: dict[str, Any]
    fuzzy_score: float
    token_coverage: float
    phase_score: float


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_cache() -> dict[str, dict[str, Any]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = Path(LOCATION_CACHE)
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            _CACHE = json.load(file)
    else:
        _CACHE = {}
    return _CACHE


def _save_cache() -> None:
    if _CACHE is None:
        return
    path = Path(LOCATION_CACHE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(_CACHE, file, indent=2)


def _append_log(path: str, message: str) -> None:
    # In production mode we suppress certain diagnostic logs to reduce disk noise
    if PRODUCTION_MODE and path in (HINT_MISMATCH_LOG, GEMINI_ARBITRATION_LOG, UNRESOLVED_LOG):
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(message + "\n")


def _token_coverage(input_tokens: set[str], candidate_canonical: str) -> float:
    if not input_tokens:
        return 0.5
    candidate_tokens = set(normalize_key(candidate_canonical).split())
    if len(input_tokens) == 1:
        return 0.5
    overlap = input_tokens & candidate_tokens
    return len(overlap) / len(input_tokens)


def _phase_score(input_phase: int | None, candidate_phase: int | None) -> tuple[float, bool]:
    if input_phase is not None:
        if candidate_phase == input_phase:
            return 1.0, False
        return 0.0, True
    if candidate_phase is None:
        return 1.0, False
    return 0.3, False


def _candidate_sort_key(candidate: Candidate) -> tuple[float, int]:
    return (candidate.confidence, _LEVEL_RANK.get(candidate.level, 0))


def _get_alias_keys_by_level(alias_map: dict[str, list[dict[str, Any]]]) -> dict[str, set[str]]:
    global _ALIAS_KEYS_BY_LEVEL
    if _ALIAS_KEYS_BY_LEVEL is not None:
        return _ALIAS_KEYS_BY_LEVEL

    keys: dict[str, set[str]] = {level: set() for level in _LEVEL_RANK}
    for alias, nodes in alias_map.items():
        for node in nodes:
            level = (node.get("level") or "").lower()
            if level in keys:
                keys[level].add(alias)
    _ALIAS_KEYS_BY_LEVEL = keys
    return keys


def _add_candidate(candidates: dict[str, Candidate], candidate: Candidate) -> None:
    key = normalize_key(candidate.canonical)
    existing = candidates.get(key)
    if existing and existing.confidence >= candidate.confidence:
        return
    candidates[key] = candidate


def _exact_candidates(term_norm: str, level: str, canonical_map: dict[str, dict[str, Any]], alias_map: dict[str, list[dict[str, Any]]]) -> list[Candidate]:
    results: list[Candidate] = []

    node = canonical_map.get(term_norm)
    if node and (node.get("level") or "").lower() == level:
        results.append(
            Candidate(
                canonical=node["canonical"],
                level=level,
                confidence=1.0,
                matched_via="canonical",
                node=node,
                fuzzy_score=1.0,
                token_coverage=1.0,
                phase_score=1.0,
            )
        )

    alias_nodes = alias_map.get(term_norm, [])
    for alias_node in alias_nodes:
        if (alias_node.get("level") or "").lower() != level:
            continue
        results.append(
            Candidate(
                canonical=alias_node["canonical"],
                level=level,
                confidence=0.93,
                matched_via="alias",
                node=alias_node,
                fuzzy_score=1.0,
                token_coverage=1.0,
                phase_score=1.0,
            )
        )

    return results


def _fuzzy_candidates(
    input_norm: str,
    input_base: str,
    input_phase: int | None,
    level: str,
    canonical_map: dict[str, dict[str, Any]],
    alias_map: dict[str, list[dict[str, Any]]],
    level_sets: dict[str, set[str]],
) -> list[Candidate]:
    set_key = _LEVEL_SET_KEYS.get(level, "")
    candidate_strings: set[str] = set(level_sets.get(set_key, set()))
    alias_keys = _get_alias_keys_by_level(alias_map).get(level, set())
    candidate_strings.update(alias_keys)

    if not candidate_strings:
        return []

    base_map: dict[str, list[str]] = {}
    for candidate_str in candidate_strings:
        base, _ = split_phase(candidate_str)
        if base:
            base_map.setdefault(base, []).append(candidate_str)

    matches = process.extract(
        input_base,
        list(base_map.keys()),
        scorer=fuzz.WRatio,
        limit=10,
        score_cutoff=50,
    )

    input_tokens = set(input_norm.split())
    results: list[Candidate] = []

    for base, score, _ in matches:
        for candidate_str in base_map.get(base, []):
            nodes: list[dict[str, Any]] = []
            node = canonical_map.get(candidate_str)
            if node and (node.get("level") or "").lower() == level:
                nodes.append(node)
            for alias_node in alias_map.get(candidate_str, []):
                if (alias_node.get("level") or "").lower() == level:
                    nodes.append(alias_node)

            for node in nodes:
                candidate_phase = split_phase(node.get("canonical", ""))[1]
                phase_score, blocked = _phase_score(input_phase, candidate_phase)
                if blocked:
                    continue

                token_score = _token_coverage(input_tokens, node.get("canonical", ""))
                fuzzy_score = score / 100.0
                confidence = (W_FUZZY * fuzzy_score) + (W_TOKEN_COVERAGE * token_score) + (W_PHASE * phase_score)

                results.append(
                    Candidate(
                        canonical=node["canonical"],
                        level=level,
                        confidence=min(confidence, 1.0),
                        matched_via="fuzzy",
                        node=node,
                        fuzzy_score=fuzzy_score,
                        token_coverage=token_score,
                        phase_score=phase_score,
                    )
                )

    return results


def _best_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    return sorted(candidates, key=_candidate_sort_key, reverse=True)[0]


def _pick_deepest(candidates: list[Candidate]) -> Candidate | None:
    filtered = [c for c in candidates if c.confidence >= MIN_CONFIDENCE]
    if not filtered:
        return None
    return sorted(
        filtered,
        key=lambda c: (_LEVEL_RANK.get(c.level, 0), c.confidence),
        reverse=True,
    )[0]


def _compute_candidates_for_level(
    hint_value: str,
    level: str,
    canonical_map: dict[str, dict[str, Any]],
    alias_map: dict[str, list[dict[str, Any]]],
    level_sets: dict[str, set[str]],
) -> list[Candidate]:
    if not hint_value:
        return []

    hint_norm = normalize_key(hint_value)
    hint_base, hint_phase = split_phase(hint_norm)

    candidates: dict[str, Candidate] = {}

    for candidate in _exact_candidates(hint_norm, level, canonical_map, alias_map):
        _add_candidate(candidates, candidate)

    for candidate in _fuzzy_candidates(hint_norm, hint_base, hint_phase, level, canonical_map, alias_map, level_sets):
        _add_candidate(candidates, candidate)

    return list(candidates.values())


def _hierarchy_validation(
    location_raw: str,
    subcommunity_candidates: list[Candidate],
    community_candidate: Candidate | None,
) -> None:
    if not subcommunity_candidates or not community_candidate:
        return

    community_norm = normalize_key(community_candidate.canonical)

    for sub_candidate in subcommunity_candidates:
        parent = sub_candidate.node.get("parent")
        if not parent:
            continue

        parent_norm = normalize_key(parent)
        if parent_norm == community_norm:
            sub_candidate.confidence = min(sub_candidate.confidence + 0.10, 1.0)
        else:
            community_candidate.confidence = max(community_candidate.confidence - 0.10, 0.0)
            message = (
                f"{_timestamp()}\t{location_raw}\t{community_candidate.canonical}"
                f"\t{parent}\t{sub_candidate.canonical}"
            )
            _append_log(HINT_MISMATCH_LOG, message)


def _depth_extension(
    location_raw: str,
    community_candidate: Candidate | None,
    subcommunity_candidates: list[Candidate],
    canonical_map: dict[str, dict[str, Any]],
) -> Candidate | None:
    if not community_candidate or community_candidate.confidence < MIN_CONFIDENCE:
        return None

    if any(c.confidence >= MIN_CONFIDENCE for c in subcommunity_candidates):
        return None

    children = community_candidate.node.get("children", [])
    if not children:
        return None

    input_norm = normalize_key(location_raw)
    input_base, input_phase = split_phase(input_norm)
    input_tokens = set(input_norm.split())

    candidates: list[Candidate] = []
    for child in children:
        node = canonical_map.get(normalize_key(child))
        if not node:
            continue
        candidate_base, candidate_phase = split_phase(node.get("canonical", ""))
        phase_score, blocked = _phase_score(input_phase, candidate_phase)
        if blocked:
            continue

        fuzzy_score = fuzz.WRatio(input_base, candidate_base) / 100.0
        token_score = _token_coverage(input_tokens, node.get("canonical", ""))
        confidence = (W_FUZZY * fuzzy_score) + (W_TOKEN_COVERAGE * token_score) + (W_PHASE * phase_score)

        candidates.append(
            Candidate(
                canonical=node["canonical"],
                level="subcommunity",
                confidence=min(confidence, 1.0),
                matched_via="depth_extension",
                node=node,
                fuzzy_score=fuzzy_score,
                token_coverage=token_score,
                phase_score=phase_score,
            )
        )

    best = _best_candidate(candidates)
    if best and best.confidence >= MIN_CONFIDENCE:
        return best
    return None


def _fallback_candidates(
    location_raw: str,
    canonical_map: dict[str, dict[str, Any]],
    alias_map: dict[str, list[dict[str, Any]]],
    level_sets: dict[str, set[str]],
) -> list[Candidate]:
    input_norm = normalize_key(location_raw)
    input_base, input_phase = split_phase(input_norm)
    candidates: dict[str, Candidate] = {}

    for level in _LEVEL_ORDER:
        for candidate in _fuzzy_candidates(input_norm, input_base, input_phase, level, canonical_map, alias_map, level_sets):
            _add_candidate(candidates, candidate)

    return list(candidates.values())


def _maybe_apply_embeddings(location_raw: str, candidates: list[Candidate]) -> list[Candidate]:
    if not USE_EMBEDDINGS or len(candidates) < 2:
        return candidates

    top = sorted(candidates, key=_candidate_sort_key, reverse=True)
    if (top[0].confidence - top[1].confidence) > AMBIGUITY_BAND:
        return candidates

    try:
        from sentence_transformers import SentenceTransformer, util
    except Exception:
        return candidates

    model = SentenceTransformer("all-MiniLM-L6-v2")
    names = [location_raw, top[0].canonical, top[1].canonical]
    embeddings = model.encode(names, convert_to_tensor=True)
    scores = util.cos_sim(embeddings[0], embeddings[1:]).tolist()[0]

    if scores[0] > scores[1]:
        top[0].confidence = min(top[0].confidence + 0.02, 1.0)
    elif scores[1] > scores[0]:
        top[1].confidence = min(top[1].confidence + 0.02, 1.0)

    return candidates


# def old_get_gemini_model() -> genai.GenerativeModel: //removed when moved to core.gemini_client, but kept for reference in case we want to re-add a local model instance in the resolver in the future
#     global _GEMINI_MODEL
#     if not GEMINI_API_KEY:
#         raise ValueError("GEMINI_API_KEY is not set.")
#     if _GEMINI_MODEL is None:
#         genai.configure(api_key=GEMINI_API_KEY)
#         _GEMINI_MODEL = genai.GenerativeModel(
#             GEMINI_MODEL,
#             system_instruction=(
#                 "You arbitrate between candidate Dubai/UAE location names. "
#                 "Return only one candidate name from the list or UNKNOWN."
#             ),
#         )
#     return _GEMINI_MODEL


def _gemini_arbitrate(location_raw: str, location_hint: dict[str, Any], candidates: list[Candidate]) -> Candidate | None:
    if not GEMINI_API_KEY:
        return None
    top_candidates = sorted(candidates, key=_candidate_sort_key, reverse=True)[:3]
    if not top_candidates:
        return None

    hint_text = ", ".join([f"{k}={v}" for k, v in (location_hint or {}).items() if v])
    candidate_lines = "\n".join(
        [f"- {c.canonical} (level={c.level}, score={c.confidence:.2f})" for c in top_candidates]
    )

    prompt = (
        "Choose the best match from the candidates below.\n"
        f"Location raw: {location_raw}\n"
        f"Location hint: {hint_text or 'none'}\n"
        "Candidates:\n"
        f"{candidate_lines}\n\n"
        "Return exactly one candidate name from the list, or UNKNOWN."
    )

    try:
        response = call_gemini(
            prompt,
            system_instruction=(
                "You arbitrate between candidate Dubai/UAE location names. "
                "Return only one candidate name from the list or UNKNOWN."
            ),
        )
    except Exception as e:
        print(f"[Resolver] Gemini arbitration failed on all models: {e}")
        return None
    text = (response.text or "").strip()

    selected = None
    for candidate in top_candidates:
        if normalize_key(candidate.canonical) == normalize_key(text):
            selected = candidate
            break

    log_message = (
        f"{_timestamp()}\t{location_raw}\t{hint_text or 'none'}"
        f"\t{text}\t{[c.canonical for c in top_candidates]}"
    )
    _append_log(GEMINI_ARBITRATION_LOG, log_message)

    return selected


def _assemble_result(candidate: Candidate | None, resolution_path: str) -> dict[str, Any]:
    if not candidate:
        return {
            "matched_canonical": None,
            "matched_level": None,
            "city": None,
            "community": None,
            "subcommunity": None,
            "property": None,
            "coords_key": None,
            "confidence": 0.0,
            "resolution_path": "unresolved",
            "location_unresolved": True,
        }

    node = candidate.node
    matched_level = candidate.level
    hierarchy = {"city": None, "community": None, "subcommunity": None, "property": None}

    current = node
    while current:
        level = (current.get("level") or "").lower()
        if level in hierarchy and not hierarchy[level]:
            hierarchy[level] = current.get("canonical")
        parent = current.get("parent")
        if not parent:
            break
        _, _, canonical_map, _ = build_or_load_tree()
        current = canonical_map.get(normalize_key(parent))

    coords = node.get("coords")
    coords_key = node.get("canonical") if coords else None
    coords_payload = None
    if coords:
        coords_payload = {"lat": coords[0], "lng": coords[1]}

    return {
        "matched_canonical": node.get("canonical"),
        "matched_level": matched_level,
        "city": hierarchy["city"],
        "community": hierarchy["community"],
        "subcommunity": hierarchy["subcommunity"],
        "property": hierarchy["property"],
        "coords_key": coords_key,
        "coords": coords_payload,
        "confidence": round(candidate.confidence, 4),
        "resolution_path": resolution_path,
        "location_unresolved": False,
    }


def _log_unresolved(location_raw: str, location_hint: dict[str, Any], candidates: list[Candidate]) -> None:
    hint_text = ", ".join([f"{k}={v}" for k, v in (location_hint or {}).items() if v])
    top = sorted(candidates, key=_candidate_sort_key, reverse=True)[:5]
    candidate_text = ", ".join([
        f"{c.canonical}:{c.level}:{c.confidence:.2f}" for c in top
    ])
    message = f"{_timestamp()}\t{location_raw}\t{hint_text or 'none'}\t{candidate_text}"
    _append_log(UNRESOLVED_LOG, message)


def resolve_location(location_raw: str, location_hint: dict[str, Any] | None = None) -> dict[str, Any]:
    cache = _load_cache()
    raw_norm = normalize_key(location_raw)
    if raw_norm and raw_norm in cache:
        cached = cache[raw_norm]
        if isinstance(cached, str):
            if cached.upper() == "UNKNOWN":
                return {
                    "matched_canonical": None,
                    "matched_level": None,
                    "city": None,
                    "community": None,
                    "subcommunity": None,
                    "property": None,
                    "coords_key": None,
                    "coords": None,
                    "confidence": 0.0,
                    "resolution_path": "cache_hit",
                    "location_unresolved": True,
                }
            return {
                "matched_canonical": cached,
                "matched_level": None,
                "city": None,
                "community": None,
                "subcommunity": None,
                "property": None,
                "coords_key": cached,
                "coords": None,
                "confidence": 1.0,
                "resolution_path": "cache_hit",
                "location_unresolved": False,
            }

        if isinstance(cached, dict):
            cached = {**cached, "resolution_path": "cache_hit"}
            return cached

    hint = location_hint or {}
    if not raw_norm and not any(hint.values()):
        result = _assemble_result(None, "unresolved")
        if raw_norm:
            cache[raw_norm] = result
            _save_cache()
        return result

    tree, alias_map, canonical_map, level_sets = build_or_load_tree()

    hint_normalized = {key: normalize_key(value) if value else "" for key, value in hint.items()}

    candidates_by_level: dict[str, list[Candidate]] = {level: [] for level in _LEVEL_ORDER}

    for level in _LEVEL_ORDER:
        hint_value = hint_normalized.get(level) or ""
        if not hint_value:
            continue
        candidates_by_level[level] = _compute_candidates_for_level(
            hint_value,
            level,
            canonical_map,
            alias_map,
            level_sets,
        )

    best_community = _best_candidate(candidates_by_level.get("community", []))
    _hierarchy_validation(location_raw, candidates_by_level.get("subcommunity", []), best_community)

    depth_extension = _depth_extension(
        location_raw,
        best_community,
        candidates_by_level.get("subcommunity", []),
        canonical_map,
    )
    if depth_extension:
        candidates_by_level["subcommunity"].append(depth_extension)

    hint_candidates = [c for level in _LEVEL_ORDER for c in candidates_by_level.get(level, [])]
    hint_candidates = _maybe_apply_embeddings(location_raw, hint_candidates)
    best_hint = _best_candidate(hint_candidates)

    resolution_path = "unresolved"
    final_candidate = None
    candidate_pool: list[Candidate] = []

    if best_hint and best_hint.confidence >= MIN_CONFIDENCE:
        final_candidate = best_hint
        candidate_pool = hint_candidates
        if depth_extension and final_candidate == depth_extension:
            resolution_path = "hint_depth_extended"
        elif best_hint.level == "community" and hint_normalized.get("community"):
            resolution_path = "hint_community_only"
        elif best_hint.level == "city" and hint_normalized.get("city"):
            resolution_path = "hint_city_only"
        else:
            resolution_path = "hint_validated"

        ordered = sorted(candidate_pool, key=_candidate_sort_key, reverse=True)
        if len(ordered) > 1 and (ordered[0].confidence - ordered[1].confidence) <= AMBIGUITY_BAND:
            gemini_choice = _gemini_arbitrate(location_raw, hint, ordered)
            if gemini_choice is None:
                # fall through to best candidate by score
                gemini_choice = ordered[0] if ordered else None
            if gemini_choice:
                final_candidate = gemini_choice
                resolution_path = "gemini_arbitration"
    else:
        fallback_candidates = _fallback_candidates(location_raw, canonical_map, alias_map, level_sets)
        fallback_candidates = _maybe_apply_embeddings(location_raw, fallback_candidates)
        best_fallback = _pick_deepest(fallback_candidates)
        if best_fallback and best_fallback.confidence >= MIN_CONFIDENCE:
            final_candidate = best_fallback
            resolution_path = "fuzzy_fallback"
            candidate_pool = hint_candidates + fallback_candidates
        else:
            final_candidate = None
            resolution_path = "unresolved"
            candidate_pool = hint_candidates + fallback_candidates

        if best_hint and best_fallback:
            if normalize_key(best_hint.canonical) != normalize_key(best_fallback.canonical):
                diff = abs(best_hint.confidence - best_fallback.confidence)
                if diff <= AMBIGUITY_BAND:
                    final_candidate = _gemini_arbitrate(location_raw, hint, hint_candidates + fallback_candidates)
                    if final_candidate:
                        resolution_path = "gemini_arbitration"

        if final_candidate and candidate_pool:
            ordered = sorted(candidate_pool, key=_candidate_sort_key, reverse=True)
            if len(ordered) > 1 and (ordered[0].confidence - ordered[1].confidence) <= AMBIGUITY_BAND:
                gemini_choice = _gemini_arbitrate(location_raw, hint, ordered)
                if gemini_choice:
                    final_candidate = gemini_choice
                    resolution_path = "gemini_arbitration"

    if final_candidate:
        result = _assemble_result(final_candidate, resolution_path)
    else:
        result = _assemble_result(None, "unresolved")
        _log_unresolved(location_raw, hint, candidate_pool)

    if raw_norm:
        cache[raw_norm] = result
        _save_cache()

    return result
