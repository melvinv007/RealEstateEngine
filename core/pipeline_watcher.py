"""
pipeline_watcher.py

Auto-runs project and location pipelines when source Excel files change.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

from core.config import (
    PROJECTS_MASTER_EXCEL,
    LOCATION_MASTER_EXCEL,
    PIPELINE_STATE_FILE,
    PIPELINE_WATCHER_LOG,
)
from ingestion import project_importer

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _log(message: str) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"{timestamp} {message}"
    print(line)
    log_path = Path(PIPELINE_WATCHER_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _log_output(prefix: str, text: str) -> None:
    if not text:
        return
    for line in text.splitlines():
        _log(f"{prefix}{line}")


def _load_state() -> dict:
    path = Path(PIPELINE_STATE_FILE)
    if not path.exists():
        state = {
            "projects_master_mtime": 0.0,
            "location_master_mtime": 0.0,
        }
        _save_state(state)
        return state

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        data = {}

    return {
        "projects_master_mtime": float(data.get("projects_master_mtime", 0.0)),
        "location_master_mtime": float(data.get("location_master_mtime", 0.0)),
    }


def _save_state(state: dict) -> None:
    path = Path(PIPELINE_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def _file_mtime(path: str) -> float | None:
    if not os.path.exists(path):
        _log(f"[Pipeline] File not found: {path}")
        return None
    return os.path.getmtime(path)


def _run_subprocess(args: list[str], label: str) -> bool:
    _log(f"[Pipeline] Running {label}: {' '.join(args)}")
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        _log(f"[Pipeline] Step failed: {label} (exit {exc.returncode})")
        _log_output("[Pipeline][stdout] ", exc.stdout or "")
        _log_output("[Pipeline][stderr] ", exc.stderr or "")
        return False

    _log_output("[Pipeline][stdout] ", result.stdout or "")
    _log_output("[Pipeline][stderr] ", result.stderr or "")
    return True


def _run_project_pipeline(state: dict) -> None:
    projects_mtime = _file_mtime(PROJECTS_MASTER_EXCEL)
    if projects_mtime is None:
        return

    last_mtime = state.get("projects_master_mtime", 0.0)
    if projects_mtime == last_mtime:
        _log("[Pipeline] Projects master unchanged; skipping.")
        return

    _log("[Pipeline] Projects master changed; starting import.")
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            result = project_importer.run_import(start_row=2, excel_path=PROJECTS_MASTER_EXCEL)
    except Exception as exc:
        _log(f"[Pipeline] Projects import failed: {exc}")
        _log_output("[Pipeline][project_importer] ", buffer.getvalue())
        return

    _log_output("[Pipeline][project_importer] ", buffer.getvalue())
    inserted = int(result.get("inserted", 0)) if isinstance(result, dict) else 0
    skipped = int(result.get("skipped", 0)) if isinstance(result, dict) else 0
    _log(f"[Pipeline] Projects import complete. {inserted} inserted, {skipped} skipped.")

    state["projects_master_mtime"] = projects_mtime
    _save_state(state)


def _run_location_pipeline(state: dict) -> None:
    location_mtime = _file_mtime(LOCATION_MASTER_EXCEL)
    if location_mtime is None:
        return

    last_mtime = state.get("location_master_mtime", 0.0)
    if location_mtime == last_mtime:
        _log("[Pipeline] Location master unchanged; skipping.")
        return

    _log("[Pipeline] Location master changed; starting pipeline.")

    steps = [
        ([sys.executable, "tools/excel_processor.py", "--start-row", "2"], "excel_processor"),
        ([sys.executable, "tools/alias_generator.py"], "alias_generator"),
        ([sys.executable, "tools/excel_to_locations.py"], "excel_to_locations"),
        ([
            sys.executable,
            "tools/geocode_locations.py",
            LOCATION_MASTER_EXCEL,
            "--start-row",
            "2",
        ], "geocode_locations"),
    ]

    for args, label in steps:
        if not _run_subprocess(args, label):
            _log(f"[Pipeline] Location pipeline stopped at {label}.")
            return

    _log(
        "[Pipeline] Location pipeline complete. Review raw_aliases.csv if needed, "
        "then re-run excel_to_locations.py for any manual corrections."
    )

    state["location_master_mtime"] = location_mtime
    _save_state(state)


def check_and_run_pipelines() -> None:
    state = _load_state()
    _run_project_pipeline(state)
    _run_location_pipeline(state)
