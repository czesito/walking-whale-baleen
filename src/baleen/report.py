"""Report and run record (spec §10): CSV report, run JSON, run index, resume lookup (§7.5).

CSV: UTF-8 with BOM, RFC 4180 quoting, CRLF, rows in plan order, written once at the end of
the run from the journal (§10.1, DR-34).
"""

from __future__ import annotations

import csv
import glob
import io
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import CSV_COLUMNS, ItemResult
from .paths import long_path

RUN_INDEX_LIMIT = 100  # §10.3, D-20


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def mtime_iso(mtime_ns: int | None) -> str:
    if mtime_ns is None:
        return ""
    return datetime.fromtimestamp(mtime_ns / 1e9, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- names


def report_name(run_id: str) -> str:
    return f"report-{run_id}.csv"


def run_json_name(run_id: str) -> str:
    return f"run-{run_id}.json"


def journal_name(run_id: str) -> str:
    return f"journal-{run_id}.sqlite"


def new_run_id(dirs: list[str], taken: set[str] | None = None, now: datetime | None = None) -> str:
    """YYYYMMDD-HHMMSS in local time, with -2, -3 ... appended if needed (§7.1)."""
    base = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    taken = taken or set()
    cand, k = base, 1
    while True:
        clash = cand in taken or any(
            os.path.exists(os.path.join(d, n))
            for d in dirs
            for n in (report_name(cand), run_json_name(cand), journal_name(cand))
        )
        if not clash:
            return cand
        k += 1
        cand = f"{base}-{k}"


# --------------------------------------------------------------------------- CSV


def write_csv(path: str, results: list[ItemResult]) -> str:
    """Write the report atomically. Returns its SHA-256."""
    import hashlib

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(CSV_COLUMNS)
    for r in sorted(results, key=lambda r: r.n):
        w.writerow(r.csv_row())
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    tmp = path + ".tmp"
    with open(long_path(tmp), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(long_path(tmp), long_path(path))
    return hashlib.sha256(data).hexdigest()


def read_csv(path: str) -> list[ItemResult]:
    with open(long_path(path), encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [ItemResult.from_csv(i, row) for i, row in enumerate(rows, start=1)]


def write_json(path: str, data: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(long_path(tmp), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(long_path(tmp), long_path(path))


def read_json(path: str) -> dict[str, Any]:
    with open(long_path(path), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- run index (§10.3)


@dataclass
class RunEntry:
    id: str
    workflow: str
    source_root: str
    output_root: str | None
    started_at: str
    finished_at: str
    state: str  # running | completed | cancelled | stopped
    counts: dict[str, int]
    total: int
    reports_dir: str  # folder holding report / run JSON / journal

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @property
    def report_path(self) -> str:
        return os.path.join(self.reports_dir, report_name(self.id))

    @property
    def run_json_path(self) -> str:
        return os.path.join(self.reports_dir, run_json_name(self.id))

    @property
    def journal_path(self) -> str:
        return os.path.join(self.reports_dir, journal_name(self.id))


class RunIndex:
    """data/runs.json: the 100 most recent runs, newest first. Older reports stay on disk."""

    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[RunEntry]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            runs = raw.get("runs", []) if isinstance(raw, dict) else []
            out = []
            for r in runs:
                try:
                    out.append(RunEntry(**r))
                except TypeError:
                    continue
            return out
        except (OSError, ValueError):
            return []

    def save(self, runs: list[RunEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(str(self.path), {"schema_version": 1, "runs": [r.to_dict() for r in runs[:RUN_INDEX_LIMIT]]})

    def upsert(self, entry: RunEntry) -> None:
        with self._lock:
            runs = [r for r in self.load() if r.id != entry.id]
            runs.insert(0, entry)
            runs.sort(key=lambda r: r.started_at, reverse=True)
            self.save(runs)

    def get(self, run_id: str) -> RunEntry | None:
        for r in self.load():
            if r.id == run_id:
                return r
        return None

    def ids(self) -> set[str]:
        return {r.id for r in self.load()}


# --------------------------------------------------------------------------- resume (§7.5, DR-18)


class ResumeIndex:
    """Prior reports in <output>/_baleen/, used to prove an existing output is Baleen's own."""

    def __init__(self, baleen_dir: str) -> None:
        self.baleen_dir = baleen_dir
        self._lock = threading.Lock()
        self._index: dict[tuple[str, str, str], set[str]] | None = None

    def _load(self) -> dict[tuple[str, str, str], set[str]]:
        idx: dict[tuple[str, str, str], set[str]] = {}
        pattern = os.path.join(glob.escape(self.baleen_dir), "report-*.csv")
        for p in sorted(glob.glob(pattern), reverse=True):  # newest first (run ids sort by time)
            try:
                rows = read_csv(p)
            except (OSError, csv.Error, UnicodeDecodeError):
                continue
            for r in rows:
                if r.output_path and r.output_sha256 and r.source_sha256:
                    idx.setdefault((r.source_path, r.source_sha256, r.output_path), set()).add(r.output_sha256)
        return idx

    def matches(self, source_path: str, source_sha256: str, output_path: str, existing_sha256: str) -> bool:
        with self._lock:
            if self._index is None:
                self._index = self._load()
            return existing_sha256 in self._index.get((source_path, source_sha256, output_path), set())
