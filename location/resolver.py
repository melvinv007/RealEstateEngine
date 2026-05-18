"""
resolver.py
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
import re
from dataclasses import dataclass, field
from datetime import datetime

import google.generativeai as genai
from rapidfuzz import fuzz, process

from core.config import (
    GEMINI_API_KEY,
    MODEL,
    LOCATIONS_CSV,
    LOCATION_CSV_CANONICAL_COLUMN,
    LOCATION_CSV_ALIASES_COLUMN,
    LOCATION_FUZZY_THRESHOLD,
    LOCATION_RESOLUTION_CACHE_FILE,
    UNRESOLVED_LOG_FILE,
    USE_GEMINI_RESOLVER_DISAMBIGUATE,
    USE_GEMINI_RESOLVER_CONFIRM,
    USE_GEMINI_RESOLVER_COLD_STEP_A,
    USE_GEMINI_RESOLVER_COLD_STEP_B,
)

genai.configure(api_key=GEMINI_API_KEY)
_gemini = genai.GenerativeModel(MODEL)

# ── Thresholds ─────────────────────────────────────────────────────────────────
# Confidence above this with a clear winner → return without Gemini
_CONFIDENCE_HIGH = 0.82
# If top-2 candidates are within this band → treat as ambiguous → Gemini disambiguate
_CONFIDENCE_AMBIGUITY_BAND = 0.08
# Confidence above this but below HIGH → confirm with Gemini before returning
_CONFIDENCE_CONFIRM = 0.65
# Below CONFIRM → cold Gemini call

# Score component weights (must sum to 1.0)
_W_FUZZY = 0.50          # how well input base matches alias base
_W_TOKEN_COVERAGE = 0.35  # what fraction of input tokens appear in candidate
_W_PHASE = 0.15           # phase agreement bonus


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class _AliasEntry:
    """One alias mapped to a canonical, pre-split into base + phase."""
    alias_norm: str          # full normalized alias key (for exact match)
    alias_base: str          # alias without trailing number
    alias_phase: int | None  # trailing number or None
    canonical: str           # canonical name this alias points to


@dataclass
class _CanonicalEntry:
    """One canonical name pre-split into base + phase."""
    canonical: str
    base: str
    phase: int | None


@dataclass
class _Candidate:
    """A scored candidate returned by Layer 2."""
    canonical: str
    confidence: float
    fuzzy_score: float
    token_coverage: float
    phase_score: float
    matched_via: str          # which alias key triggered this


# ── Global state ───────────────────────────────────────────────────────────────

_alias_entries: list[_AliasEntry] = []
_alias_map: dict[str, str] = {}          # normalized_key → canonical (for exact match)
_canonical_entries: list[_CanonicalEntry] = []
_canonical_list: list[str] = []
_canonical_set: set[str] = set()
_resolution_cache: dict[str, str] = {}


# ── Utilities ──────────────────────────────────────────────────────────────────

_NUM_SUFFIX_RE = re.compile(r'^(.*?)\s*(\d+)\s*$')


def normalize_key(s: str) -> str:
    """
    Lowercase → strip → remove dots → collapse whitespace.
    'D.S.O' → 'dso', 'al  barsha' → 'al barsha', 'J.V.C.' → 'jvc'
    """
    s = s.lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s)
    return s


def _split_phase(s: str) -> tuple[str, int | None]:
    """
    Split a normalized string into (base, phase_number).
    'arabian ranches 3' → ('arabian ranches', 3)
    'ar3'               → ('ar', 3)         ← handles concatenated too
    'business bay'      → ('business bay', None)
    'jvc'               → ('jvc', None)
    """
    # First try standard trailing number with optional space
    m = _NUM_SUFFIX_RE.match(s.strip())
    if m:
        base = m.group(1).strip()
        phase = int(m.group(2))
        if base:  # guard against input being just a number
            return base, phase

    # Try concatenated: last char(s) are digits, rest is alpha
    # e.g. 'ar3', 'phase3'
    concat = re.match(r'^([a-z][a-z\s]*)(\d+)$', s.strip())
    if concat:
        base = concat.group(1).strip()
        phase = int(concat.group(2))
        if base:
            return base, phase

    return s.strip(), None


def _token_coverage(input_tokens: set[str], candidate_canonical: str) -> float:
    """
    What fraction of input tokens appear in the candidate canonical name?
    input: {'marina', 'gate'}  candidate: 'Marina Gate'    → 1.0
    input: {'marina', 'gate'}  candidate: 'Dubai Marina'   → 0.5
    input: {'jvc'}             candidate: 'Jumeirah Village Circle' → 0.0 (abbreviation)
    Short single-token inputs get a neutral 0.5 to avoid over-penalizing abbreviations.
    """
    if not input_tokens:
        return 0.5
    candidate_tokens = set(normalize_key(candidate_canonical).split())
    if len(input_tokens) == 1:
        # Single token — coverage not very meaningful, return neutral
        return 0.5
    overlap = input_tokens & candidate_tokens
    return len(overlap) / len(input_tokens)


def _phase_agreement_score(input_phase: int | None, canonical_entry: _CanonicalEntry) -> float:
    """
    Returns a score 0.0–1.0 for how well phases agree, and a hard-block flag.
    Returns -1.0 if this candidate should be completely blocked.

    Rules:
    - input has phase N, candidate has phase N      → 1.0  (perfect)
    - input has no phase, candidate has no phase    → 1.0  (perfect)
    - input has phase N, candidate has no phase     → BLOCK (-1.0)
    - input has no phase, candidate has phase N     → 0.3  (penalize, don't block —
                                                      user might have omitted phase)
    - input has phase N, candidate has phase M≠N    → BLOCK (-1.0)
    """
    c_phase = canonical_entry.phase

    if input_phase is not None:
        if c_phase == input_phase:
            return 1.0
        else:
            return -1.0  # hard block — wrong phase or no phase

    else:  # input has no phase
        if c_phase is None:
            return 1.0
        else:
            return 0.3  # candidate has a phase but input didn't specify — penalize


# ── Loaders ────────────────────────────────────────────────────────────────────

def _load_locations() -> None:
    global _alias_entries, _alias_map, _canonical_entries, _canonical_list, _canonical_set

    if not os.path.exists(LOCATIONS_CSV):
        print(f"[Resolver] Warning: {LOCATIONS_CSV} not found.")
        return

    with open(LOCATIONS_CSV, "r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            return

        header_map = {name.strip().lower(): name for name in reader.fieldnames if name}
        canonical_key = header_map.get(LOCATION_CSV_CANONICAL_COLUMN.lower())
        alias_key = header_map.get(LOCATION_CSV_ALIASES_COLUMN.lower())

        if not canonical_key or not alias_key:
            print(f"[Resolver] Warning: missing required columns in {LOCATIONS_CSV}.")
            return

        alias_entries: list[_AliasEntry] = []
        alias_map: dict[str, str] = {}
        canonical_entries: list[_CanonicalEntry] = []
        canonical_names: set[str] = set()

        for row in reader:
            canonical = (row.get(canonical_key) or "").strip()
            if not canonical:
                continue

            canonical_names.add(canonical)

            # Build canonical entry with phase split
            c_norm = normalize_key(canonical)
            c_base, c_phase = _split_phase(c_norm)
            canonical_entries.append(_CanonicalEntry(
                canonical=canonical,
                base=c_base,
                phase=c_phase,
            ))

            # Canonical name itself as an alias
            alias_map[c_norm] = canonical
            a_base, a_phase = _split_phase(c_norm)
            alias_entries.append(_AliasEntry(
                alias_norm=c_norm,
                alias_base=a_base,
                alias_phase=a_phase,
                canonical=canonical,
            ))

            # All explicit aliases
            aliases_raw = row.get(alias_key) or ""
            for alias in aliases_raw.split(","):
                alias = alias.strip()
                if not alias:
                    continue
                a_norm = normalize_key(alias)
                alias_map[a_norm] = canonical
                a_base, a_phase = _split_phase(a_norm)
                alias_entries.append(_AliasEntry(
                    alias_norm=a_norm,
                    alias_base=a_base,
                    alias_phase=a_phase,
                    canonical=canonical,
                ))

        _alias_entries = alias_entries
        _alias_map = alias_map
        _canonical_entries = canonical_entries
        _canonical_list = sorted(canonical_names)
        _canonical_set = set(_canonical_list)

    print(f"[Resolver] Loaded {len(_canonical_list)} canonicals, "
          f"{len(_alias_entries)} alias entries.")


def _load_resolution_cache() -> None:
    global _resolution_cache
    if os.path.exists(LOCATION_RESOLUTION_CACHE_FILE):
        try:
            with open(LOCATION_RESOLUTION_CACHE_FILE, "r", encoding="utf-8") as f:
                _resolution_cache = json.load(f)
        except Exception:
            _resolution_cache = {}
    else:
        _resolution_cache = {}


def _save_resolution_cache() -> None:
    with open(LOCATION_RESOLUTION_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(_resolution_cache, f, indent=2)


# ── Logging ────────────────────────────────────────────────────────────────────

def _log_unresolved(
    raw_input: str,
    candidates: list[_Candidate],
    gemini_result: str | None,
) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(UNRESOLVED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] UNRESOLVED: '{raw_input}'\n")
        if candidates:
            f.write(f"  Top candidates:\n")
            for c in candidates[:3]:
                f.write(
                    f"    '{c.canonical}' confidence={c.confidence:.2f} "
                    f"(fuzzy={c.fuzzy_score:.0f}, token_cov={c.token_coverage:.2f}, "
                    f"phase={c.phase_score:.2f}) via alias '{c.matched_via}'\n"
                )
        else:
            f.write(f"  No candidates generated.\n")
        f.write(f"  Gemini returned: {gemini_result or 'NOT_CALLED'}\n\n")


# ── Layer 2: Candidate generation + scoring ────────────────────────────────────

def _generate_candidates(norm_input: str) -> list[_Candidate]:
    """
    Core of the new system.
    1. Split input into {base, phase}
    2. Fuzzy match input_base against every alias_base → top N
    3. Score each by fuzzy + token_coverage + phase_agreement
    4. Deduplicate by canonical (keep best score per canonical)
    5. Return sorted by confidence descending
    """
    input_base, input_phase = _split_phase(norm_input)
    input_tokens = set(input_base.split())

    # Collect all alias bases for fuzzy matching
    alias_bases = [e.alias_base for e in _alias_entries]

    # Get top-N fuzzy matches on base (generous cutoff, we filter by confidence later)
    raw_results = process.extract(
        input_base,
        alias_bases,
        scorer=fuzz.WRatio,
        limit=10,
        score_cutoff=50,  # intentionally low — confidence scoring does the real filtering
    )

    # Map results back to alias entries (by index)
    # process.extract returns (match, score, index)
    scored: dict[str, _Candidate] = {}  # canonical → best candidate

    for matched_base, fuzzy_score, idx in raw_results:
        entry = _alias_entries[idx]

        # Phase agreement
        # Find the canonical entry for this alias's canonical name
        c_entry = next(
            (ce for ce in _canonical_entries if ce.canonical == entry.canonical),
            None,
        )
        if c_entry is None:
            continue

        phase_score = _phase_agreement_score(input_phase, c_entry)
        if phase_score == -1.0:
            # Hard block — wrong phase
            continue

        # Token coverage
        token_cov = _token_coverage(input_tokens, entry.canonical)

        # Combined confidence
        fuzzy_norm = fuzzy_score / 100.0
        confidence = (
            _W_FUZZY * fuzzy_norm
            + _W_TOKEN_COVERAGE * token_cov
            + _W_PHASE * phase_score
        )

        candidate = _Candidate(
            canonical=entry.canonical,
            confidence=confidence,
            fuzzy_score=fuzzy_score,
            token_coverage=token_cov,
            phase_score=phase_score,
            matched_via=entry.alias_norm,
        )

        # Keep best candidate per canonical
        existing = scored.get(entry.canonical)
        if existing is None or candidate.confidence > existing.confidence:
            scored[entry.canonical] = candidate

    result = sorted(scored.values(), key=lambda c: c.confidence, reverse=True)
    return result


def _decide_from_candidates(
    candidates: list[_Candidate],
) -> tuple[str | None, str]:
    """
    Given scored candidates, decide what to do.
    Returns (resolved_canonical_or_None, decision_reason).

    decision_reason is one of:
      'direct'       — high confidence, clear winner
      'ambiguous'    — top candidates too close, send to Gemini
      'confirm'      — medium confidence, ask Gemini to confirm
      'low'          — nothing useful, cold Gemini call
    """
    if not candidates:
        return None, "low"

    top = candidates[0]

    if top.confidence >= _CONFIDENCE_HIGH:
        # Check if second candidate is close — if so, ambiguous
        if len(candidates) > 1:
            second = candidates[1]
            if (top.confidence - second.confidence) <= _CONFIDENCE_AMBIGUITY_BAND:
                return None, "ambiguous"
        # Clear winner
        return top.canonical, "direct"

    if top.confidence >= _CONFIDENCE_CONFIRM:
        # Medium confidence — ask Gemini to confirm before committing
        return None, "confirm"

    return None, "low"


# ── Layer 3: Targeted Gemini ───────────────────────────────────────────────────

def _gemini_disambiguate(raw_input: str, candidates: list[_Candidate]) -> str | None:
    """
    Mode A: We have 2+ close candidates. Ask Gemini to pick.
    e.g. 'marina gate' → candidates: [Marina Gate, Dubai Marina]
    """
    if not USE_GEMINI_RESOLVER_DISAMBIGUATE:
        return None

    options = "\n".join(
        f"  {i+1}. {c.canonical} (confidence={c.confidence:.2f})"
        for i, c in enumerate(candidates[:4])
    )
    prompt = (
        "You are a Dubai real estate location expert.\n"
        f"A broker wrote: '{raw_input}'\n\n"
        "Which of these Dubai areas did they most likely mean?\n"
        f"{options}\n\n"
        "Reply with EXACTLY the name as written above, or UNKNOWN if none fit.\n"
        "No explanation. No punctuation. Just the name or UNKNOWN."
    )
    try:
        resp = _gemini.generate_content(prompt)
        answer = resp.text.strip()
    except Exception:
        return None

    if answer == "UNKNOWN":
        return None

    # Validate answer is one of the candidates we offered
    for c in candidates[:4]:
        if c.canonical.lower() == answer.lower():
            print(f"[Resolver] Gemini disambiguated: '{raw_input}' → '{c.canonical}'")
            return c.canonical

    # Gemini returned something not in our list — try alias map
    answer_norm = normalize_key(answer)
    if answer_norm in _alias_map:
        resolved = _alias_map[answer_norm]
        print(f"[Resolver] Gemini disambiguate (alias fallback): '{raw_input}' → '{resolved}'")
        return resolved

    return None


def _gemini_confirm(raw_input: str, candidate: _Candidate) -> str | None:
    """
    Mode B: We have one medium-confidence candidate. Ask Gemini to confirm or correct.
    """
    if not USE_GEMINI_RESOLVER_CONFIRM:
        return None

    canonical_list_text = "\n".join(_canonical_list)
    prompt = (
        "You are a Dubai real estate location expert.\n"
        f"A broker wrote: '{raw_input}'\n\n"
        f"I think this refers to: '{candidate.canonical}' "
        f"(confidence={candidate.confidence:.2f})\n\n"
        "Is this correct? If yes, reply with that exact name.\n"
        "If not, reply with the correct name from this list, or UNKNOWN:\n"
        f"{canonical_list_text}\n\n"
        "Reply with EXACTLY one name from the list, or UNKNOWN.\n"
        "No explanation. No punctuation."
    )
    try:
        resp = _gemini.generate_content(prompt)
        answer = resp.text.strip()
    except Exception:
        return None

    if answer == "UNKNOWN":
        return None

    if answer in _canonical_set:
        print(f"[Resolver] Gemini confirmed/corrected: '{raw_input}' → '{answer}'")
        return answer

    answer_norm = normalize_key(answer)
    if answer_norm in _alias_map:
        resolved = _alias_map[answer_norm]
        print(f"[Resolver] Gemini confirm (alias fallback): '{raw_input}' → '{resolved}'")
        return resolved

    return None


def _gemini_cold(raw_input: str) -> str | None:
    """
    Mode C: Low confidence, no useful candidates. Original two-step Gemini behavior.
    Step A: open-ended → Step B: constrained to canonical list.
    """
    if not USE_GEMINI_RESOLVER_COLD_STEP_A:
        return None

    prompt_a = (
        "You are a Dubai real estate expert.\n"
        "What place in Dubai could this refer to? Reply with ONLY the place name, "
        "or UNKNOWN if you are not sure.\n"
        "No explanation. No punctuation. Just the name or UNKNOWN.\n"
        f"Input: {raw_input}"
    )
    try:
        resp_a = _gemini.generate_content(prompt_a)
        step_a = resp_a.text.strip()
    except Exception:
        return None

    if step_a == "UNKNOWN" or not step_a:
        print(f"[Resolver] Gemini cold Step A: UNKNOWN for '{raw_input}'")
        return None

    # Check if Step A answer already canonical or in alias map
    if step_a in _canonical_set:
        print(f"[Resolver] Gemini cold Step A resolved: '{raw_input}' → '{step_a}'")
        return step_a

    step_a_norm = normalize_key(step_a)
    if step_a_norm in _alias_map:
        resolved = _alias_map[step_a_norm]
        print(f"[Resolver] Gemini cold Step A (alias): '{raw_input}' → '{resolved}'")
        return resolved

    if not USE_GEMINI_RESOLVER_COLD_STEP_B:
        print(f"[Resolver] Gemini cold Step B disabled for '{raw_input}'")
        return None

    # Step B — constrain to canonical list
    print(f"[Resolver] Gemini cold Step A '{step_a}' not in list, trying Step B...")
    canonical_list_text = "\n".join(_canonical_list)
    prompt_b = (
        "You are a Dubai real estate location resolver.\n"
        f"A location was described as: '{step_a}'\n"
        "Which official Dubai area from the list below best matches this?\n"
        "Reply with EXACTLY one name from the list, or UNKNOWN if none fits.\n"
        "No explanation. No punctuation. Just the name or UNKNOWN.\n"
        f"CANONICAL LIST:\n{canonical_list_text}\n\n"
        f"Input: {step_a}"
    )
    try:
        resp_b = _gemini.generate_content(prompt_b)
        step_b = resp_b.text.strip()
    except Exception:
        return None

    if step_b == "UNKNOWN" or step_b not in _canonical_set:
        print(f"[Resolver] Gemini cold Step B failed for '{raw_input}' (got '{step_b}')")
        return None

    print(f"[Resolver] Gemini cold Step B resolved: '{raw_input}' → '{step_b}'")
    return step_b


# ── Main public API ────────────────────────────────────────────────────────────

def resolve_location(raw: str) -> str | None:
    """
    Resolves a raw location string to a canonical Dubai area name.
    Returns canonical name if resolved, None if unresolved.
    Caches both successes and failures.
    """
    if not raw:
        return None
    raw_input = str(raw).strip()
    if not raw_input:
        return None

    norm = normalize_key(raw_input)

    # ── L0: Cache ──────────────────────────────────────────────────────────────
    if norm in _resolution_cache:
        cached = _resolution_cache[norm]
        return None if cached == "UNKNOWN" else cached

    # ── L1: Exact alias match ──────────────────────────────────────────────────
    if norm in _alias_map:
        resolved = _alias_map[norm]
        _resolution_cache[norm] = resolved
        _save_resolution_cache()
        print(f"[Resolver] Exact match: '{raw_input}' → '{resolved}'")
        return resolved

    # ── L2: Candidate generation + confidence scoring ──────────────────────────
    candidates = _generate_candidates(norm)

    if candidates:
        print(f"[Resolver] Top candidates for '{raw_input}':")
        for c in candidates[:4]:
            print(
                f"  '{c.canonical}' conf={c.confidence:.2f} "
                f"(fuzzy={c.fuzzy_score:.0f}, tok_cov={c.token_coverage:.2f}, "
                f"phase={c.phase_score:.2f})"
            )

    resolved_canonical, decision = _decide_from_candidates(candidates)

    if decision == "direct" and resolved_canonical:
        print(f"[Resolver] High-confidence direct: '{raw_input}' → '{resolved_canonical}'")
        _resolution_cache[norm] = resolved_canonical
        _save_resolution_cache()
        return resolved_canonical

    # ── L3: Gemini (targeted) ──────────────────────────────────────────────────
    gemini_result_str = None
    resolved = None

    if decision == "ambiguous":
        if USE_GEMINI_RESOLVER_DISAMBIGUATE:
            print(f"[Resolver] Ambiguous candidates for '{raw_input}', escalating to Gemini...")
            resolved = _gemini_disambiguate(raw_input, candidates)
            gemini_result_str = resolved or "UNKNOWN"
        if not resolved and USE_GEMINI_RESOLVER_COLD_STEP_A:
            print(f"[Resolver] Ambiguous resolution failed, cold Gemini call for '{raw_input}'...")
            resolved = _gemini_cold(raw_input)
            gemini_result_str = resolved or "UNKNOWN"

    elif decision == "confirm":
        if USE_GEMINI_RESOLVER_CONFIRM:
            print(f"[Resolver] Medium confidence for '{raw_input}', asking Gemini to confirm...")
            resolved = _gemini_confirm(raw_input, candidates[0])
            gemini_result_str = resolved or "UNKNOWN"
        if not resolved and USE_GEMINI_RESOLVER_COLD_STEP_A:
            print(f"[Resolver] Confirm failed, cold Gemini call for '{raw_input}'...")
            resolved = _gemini_cold(raw_input)
            gemini_result_str = resolved or "UNKNOWN"

    else:  # "low"
        if USE_GEMINI_RESOLVER_COLD_STEP_A:
            print(f"[Resolver] Low confidence for '{raw_input}', cold Gemini call...")
            resolved = _gemini_cold(raw_input)
            gemini_result_str = resolved or "UNKNOWN"

    if gemini_result_str is None:
        gemini_result_str = "DISABLED"

    if resolved:
        _resolution_cache[norm] = resolved
        _save_resolution_cache()
        return resolved

    # ── L4: Unresolved ─────────────────────────────────────────────────────────
    _log_unresolved(raw_input, candidates, gemini_result_str)
    _resolution_cache[norm] = "UNKNOWN"
    _save_resolution_cache()
    return None


# ── Init ───────────────────────────────────────────────────────────────────────

_load_locations()
_load_resolution_cache()