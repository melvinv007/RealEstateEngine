"""
alias_generator.py

Generate broker-style aliases for Dubai/UAE locations using Gemini.
Reads cache/unique_names.json and writes data/raw_aliases.csv.

Supports --start-row N (1-based index into unique_names.json order).
Resumable: entries already present in data/raw_aliases.csv are skipped.
"""

import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.gemini_client import call_gemini

# ---- Config -----------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
INPUT_PATH = "cache/unique_names.json"
OUTPUT_PATH = "data/raw_aliases.csv"
PROGRESS_PATH = "cache/alias_gen_progress.json"
ERROR_LOG = "cache/alias_gen_errors.log"
DELAY_BETWEEN_CALLS = 2
TIER3_SKIP = True

SYSTEM_PROMPT = (
    "You generate location aliases for Dubai/UAE real estate broker messages. "
    "Brokers use WhatsApp and type quickly."
)

USER_PROMPT_TEMPLATE = (
    "Generate aliases for this Dubai/UAE location:\n"
    "Name: {canonical}\n"
    "Level: {level}  (city/community/subcommunity/property)\n"
    "Parent: {parent}\n\n"
    "Generate aliases in these categories:\n"
    "- Common abbreviations brokers use (initials, shortened forms)\n"
    "- Common shortforms (dropping words, truncations)\n"
    "- Typical spelling mistakes (wrong vowels, missing/double letters, transpositions)\n"
    "- Concatenations (words joined without space)\n"
    "- With parent name prepended or appended where natural\n"
    "- Numeric variants if name contains numbers\n\n"
    "Rules:\n"
    "- All lowercase\n"
    "- Only aliases a real Dubai broker would plausibly write or type\n"
    "- Do not include the canonical name itself\n"
    "- Do not invent implausible aliases\n"
    "- Return ONLY a comma-separated list, nothing else, no explanation"
)


# ---- Helpers ----------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _key(*parts: str) -> tuple[str, str, str]:
    canonical, level, parent = parts
    return (canonical.lower(), level.lower(), parent.lower())


def _join_parent(parts: list[str]) -> str:
    cleaned = [part for part in parts if part]
    return ", ".join(cleaned)


def _load_existing(output_path: Path) -> set[tuple[str, str, str]]:
    if not output_path.exists():
        return set()

    existing: set[tuple[str, str, str]] = set()
    with output_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            canonical = _normalize(row.get("canonical_name"))
            level = _normalize(row.get("level"))
            parent = _normalize(row.get("parent"))
            if canonical and level:
                existing.add(_key(canonical, level, parent))
    return existing


def _ensure_output_header(output_path: Path) -> None:
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["canonical_name", "level", "parent", "aliases"])


def _append_row(output_path: Path, canonical: str, level: str, parent: str, aliases: str) -> None:
    with output_path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([canonical, level, parent, aliases])


def _save_progress(last_processed: str | None, completed_count: int, remaining_count: int, reason: str) -> None:
    path = Path(PROGRESS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _timestamp(),
        "last_processed": last_processed,
        "completed_count": completed_count,
        "remaining_count": remaining_count,
        "stopped_reason": reason,
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def _log_error(name: str, level: str, parent: str, error: Exception) -> None:
    path = Path(ERROR_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    message = str(error)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"{_timestamp()}\t{name}\t{level}\t{parent}\t{message}\n")


def _is_quota_error(error: Exception) -> bool:
    message = str(error).lower()
    quota_markers = ["429", "quota", "resource exhausted", "resource_exhausted", "rate limit"]
    return any(marker in message for marker in quota_markers)


def _parse_aliases(raw_text: str, canonical: str) -> str:
    if not raw_text:
        return ""
    canonical_norm = canonical.strip().lower()
    cleaned = raw_text.replace("\n", ",")
    parts = [part.strip().lower() for part in cleaned.split(",") if part.strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part == canonical_norm:
            continue
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return ", ".join(deduped)


def _build_queue(data: dict[str, Any]) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(canonical: str, level: str, parent: str) -> None:
        canonical = _normalize(canonical)
        parent = _normalize(parent)
        if not canonical:
            return
        key = _key(canonical, level, parent)
        if key in seen:
            return
        seen.add(key)
        queue.append({"canonical": canonical, "level": level, "parent": parent})

    for city in data.get("cities", []):
        _add(str(city), "city", "")

    for entry in data.get("communities", []):
        canonical = _normalize(entry.get("canonical"))
        parent_city = _normalize(entry.get("parent_city"))
        _add(canonical, "community", parent_city)

    for entry in data.get("subcommunities", []):
        canonical = _normalize(entry.get("canonical"))
        parent_community = _normalize(entry.get("parent_community"))
        parent_city = _normalize(entry.get("parent_city"))
        parent = _join_parent([parent_community, parent_city])
        _add(canonical, "subcommunity", parent)

    for entry in data.get("properties", []):
        if TIER3_SKIP and entry.get("tier3"):
            continue
        canonical = _normalize(entry.get("canonical"))
        parent_subcommunity = _normalize(entry.get("parent_subcommunity"))
        parent_community = _normalize(entry.get("parent_community"))
        parent_city = _normalize(entry.get("parent_city"))
        parent = _join_parent([parent_subcommunity, parent_community, parent_city])
        _add(canonical, "property", parent)

    return queue


# def _generate_aliases(model: genai.GenerativeModel, canonical: str, level: str, parent: str) -> str:
#     prompt = USER_PROMPT_TEMPLATE.format(canonical=canonical, level=level, parent=parent)
#     response = model.generate_content(prompt)
#     text = response.text if hasattr(response, "text") else str(response)
#     return _parse_aliases(text, canonical)

def _generate_aliases(canonical: str, level: str, parent: str) -> str:
    prompt = USER_PROMPT_TEMPLATE.format(canonical=canonical, level=level, parent=parent)
    response = call_gemini(prompt, system_instruction=SYSTEM_PROMPT)
    text = response.text if hasattr(response, "text") else str(response)
    return _parse_aliases(text, canonical)


# ---- Main -------------------------------------------------------------------

def run(start_row: int = 1) -> int:
    input_path = Path(INPUT_PATH)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_PATH}")

    with input_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    queue = _build_queue(data)
    if start_row < 1:
        start_row = 1
    if start_row > 1:
        print(f"[AliasGen] Skipping entries 1–{start_row - 1} (--start-row {start_row})")
    queue = queue[start_row - 1:]
    total = len(queue)

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in the environment.")


    output_path = Path(OUTPUT_PATH)
    # Resumable behavior: skip any canonical already present in raw_aliases.csv.
    existing = _load_existing(output_path)
    _ensure_output_header(output_path)

    existing_in_scope = sum(1 for item in queue if _key(item["canonical"], item["level"], item["parent"]) in existing)
    completed_count = existing_in_scope
    processed_count = existing_in_scope
    last_processed: str | None = None

    for item in queue:
        canonical = item["canonical"]
        level = item["level"]
        parent = item["parent"]

        if _key(canonical, level, parent) in existing:
            continue

        try:
            aliases = _generate_aliases(canonical, level, parent)
            _append_row(output_path, canonical, level, parent, aliases)
            completed_count += 1
            processed_count += 1
            last_processed = canonical

        except KeyboardInterrupt:
            remaining = max(total - processed_count, 0)

            _save_progress(
                last_processed,
                completed_count,
                remaining,
                "keyboard_interrupt"
            )

            print(f"\n{_timestamp()} Interrupted by user.")
            return 0

        except Exception as exc:
            if _is_quota_error(exc):
                remaining = total - processed_count
                print(f"{_timestamp()} Quota exceeded. Stopping gracefully.")
                _save_progress(last_processed or canonical, completed_count, remaining, "quota_exceeded")
                return 0

            _log_error(canonical, level, parent, exc)
            processed_count += 1
            last_processed = canonical
        finally:
            if processed_count % 50 == 0:
                remaining = max(total - processed_count, 0)
                _save_progress(last_processed, completed_count, remaining, "running")
            if processed_count % 10 == 0 and processed_count > 0:
                remaining = max(total - processed_count, 0)
                print(f"Progress: {processed_count}/{total} completed ({remaining} remaining)")

        time.sleep(DELAY_BETWEEN_CALLS)

    print(f"Progress: {processed_count}/{total} completed (0 remaining)")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Generate location aliases using Gemini")
    parser.add_argument("--start-row", type=int, default=1, help="Start index (1-based) into unique_names.json")
    args = parser.parse_args()

    try:
        return run(start_row=args.start_row)

    except Exception as exc:
        remaining = 0
        reason = f"unexpected_error: {exc}"
        _save_progress(None, 0, remaining, reason)
        print(f"{_timestamp()} Unexpected error. Stopping gracefully.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
