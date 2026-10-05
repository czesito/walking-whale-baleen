"""Run journal and results index (spec §10.4, DR-34).

Every finished item is committed to `journal-<run-id>.sqlite` (WAL, one commit per item).
During the run it is the crash-safe record; afterwards it is the index the run page queries
(filters, sorting, paging, inspector), so pages stay fast at 50,000 rows. The CSV remains the
archival record; a missing journal is rebuilt from the CSV.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .model import CSV_COLUMNS, STATUS_RANK, ItemResult, PlanItem, Status
from .paths import fold_key

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan (
    n INTEGER PRIMARY KEY, source_path TEXT NOT NULL, materialise INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    n INTEGER PRIMARY KEY,
    run_id TEXT, source_path TEXT, source_size INTEGER, source_mtime TEXT, source_sha256 TEXT,
    source_format TEXT, category TEXT, action TEXT, method TEXT, output_path TEXT,
    output_size INTEGER, output_sha256 TEXT, status TEXT, reason TEXT, checks TEXT, message TEXT,
    rank INTEGER NOT NULL, search TEXT NOT NULL, finished_seq INTEGER NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS results_rank ON results(rank, n);
CREATE INDEX IF NOT EXISTS results_status ON results(status, n);
CREATE INDEX IF NOT EXISTS results_seq ON results(finished_seq);
"""

_RESULT_COLS = list(CSV_COLUMNS)
_INSERT_SQL = (
    f"INSERT OR REPLACE INTO results(n, {', '.join(_RESULT_COLS)}, rank, search, finished_seq, finished_at) "  # noqa: S608
    f"VALUES (?, {', '.join('?' for _ in _RESULT_COLS)}, ?, ?, ?, ?)"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Journal:
    """Writer + reader. Writes are serialised by a lock; readers use short-lived connections."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._seq = 0

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, path: str, meta: dict[str, Any], plan: Iterable[PlanItem]) -> Journal:
        if os.path.exists(path):
            raise FileExistsError(path)
        j = cls(path)
        conn = j._writer()
        with j._lock:
            conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                             [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
            conn.executemany(
                "INSERT INTO plan(n, source_path, materialise, data) VALUES (?, ?, ?, ?)",
                [(it.n, it.source_path, 1 if it.materialise else 0,
                  json.dumps(it.to_dict(), ensure_ascii=False)) for it in plan],
            )
            conn.commit()
        return j

    @classmethod
    def open(cls, path: str) -> Journal:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        j = cls(path)
        with j._read() as c:
            row = c.execute("SELECT COALESCE(MAX(finished_seq), 0) FROM results").fetchone()
            j._seq = int(row[0])
        return j

    def _writer(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def _read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ writes

    def commit(self, r: ItemResult) -> None:
        """Commit one finished item (crash-safe record, §5.4 Report)."""
        conn = self._writer()
        with self._lock:
            self._seq += 1
            r.finished_at = r.finished_at or _now()
            rank = STATUS_RANK.get(Status(r.status), 9)
            search = fold_key(f"{r.source_path}\n{r.output_path}")
            values = [getattr(r, c) for c in _RESULT_COLS]
            conn.execute(_INSERT_SQL, [r.n, *values, rank, search, self._seq, r.finished_at])
            conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        conn = self._writer()
        with self._lock:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (key, json.dumps(value, ensure_ascii=False)))
            conn.commit()

    # ------------------------------------------------------------------ reads

    def meta(self) -> dict[str, Any]:
        with self._read() as c:
            return {row["key"]: json.loads(row["value"]) for row in c.execute("SELECT key, value FROM meta")}

    def plan_items(self) -> list[PlanItem]:
        with self._read() as c:
            return [PlanItem.from_dict(json.loads(row["data"]))
                    for row in c.execute("SELECT data FROM plan ORDER BY n")]

    @staticmethod
    def _row(row: sqlite3.Row) -> ItemResult:
        r = ItemResult(n=row["n"], run_id=row["run_id"] or "", source_path=row["source_path"] or "")
        for c in _RESULT_COLS:
            setattr(r, c, row[c] if row[c] is not None else ("" if c not in ("source_size", "output_size")
                                                                else None))
        r.finished_at = row["finished_at"] or ""
        return r

    def get(self, n: int) -> ItemResult | None:
        with self._read() as c:
            row = c.execute("SELECT * FROM results WHERE n = ?", (n,)).fetchone()
            return self._row(row) if row else None

    def done_count(self) -> int:
        with self._read() as c:
            return int(c.execute("SELECT COUNT(*) FROM results").fetchone()[0])

    def finished_ns(self) -> set[int]:
        with self._read() as c:
            return {int(r[0]) for r in c.execute("SELECT n FROM results")}

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {"by_status": {}, "by_category": {}, "by_action": {}}
        with self._read() as c:
            for col, key in (("status", "by_status"), ("category", "by_category"), ("action", "by_action")):
                for row in c.execute(f"SELECT {col} AS k, COUNT(*) AS v FROM results GROUP BY {col}"):  # noqa: S608
                    out[key][row["k"] or ""] = int(row["v"])
        return out

    def latest(self, k: int = 6) -> list[ItemResult]:
        with self._read() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM results ORDER BY finished_seq DESC LIMIT ?", (k,))]

    def query(self, *, statuses: list[str] | None = None, q: str = "", page: int = 1,
              page_size: int = 100) -> tuple[list[ItemResult], int]:
        """Results table (UI-R4): severity then plan (source path) order; text filter on paths."""
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args += statuses
        if q:
            where.append("instr(search, ?) > 0")
            args.append(fold_key(q))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        page = max(1, page)
        with self._read() as c:
            total = int(c.execute(f"SELECT COUNT(*) FROM results {clause}", args).fetchone()[0])  # noqa: S608
            rows = c.execute(
                f"SELECT * FROM results {clause} ORDER BY rank, n LIMIT ? OFFSET ?",  # noqa: S608
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
        return [self._row(r) for r in rows], total

    def neighbours(self, n: int, *, statuses: list[str] | None = None, q: str = "") -> tuple[int | None, int | None]:
        """Previous / next row in table order, for ↑↓ in the inspector."""
        rows, _ = self.query(statuses=statuses, q=q, page=1, page_size=1_000_000)
        ns = [r.n for r in rows]
        if n not in ns:
            return None, None
        i = ns.index(n)
        return (ns[i - 1] if i > 0 else None), (ns[i + 1] if i + 1 < len(ns) else None)

    def results_in_plan_order(self) -> list[ItemResult]:
        with self._read() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM results ORDER BY n")]

    # ------------------------------------------------------------------ rebuild

    @classmethod
    def rebuild_from_rows(cls, path: str, meta: dict[str, Any], results: list[ItemResult]) -> Journal:
        """Rebuild a missing journal from the CSV report (§10.4)."""
        j = cls.create(path, meta, [])
        for r in results:
            j.commit(r)
        return j
