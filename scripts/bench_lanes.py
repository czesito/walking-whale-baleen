"""Per-category timeline of a benchmark run from its kept journal (when each category finished).

    python scripts/bench_lanes.py <kept-run-folder>
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    folder = Path(sys.argv[1])
    j = sorted(folder.glob("journal-*.sqlite"))[0]
    c = sqlite3.connect(j)
    rows = c.execute("SELECT category, finished_at FROM results WHERE finished_at != ''").fetchall()
    ts = [(cat, datetime.fromisoformat(t.replace("Z", "+00:00"))) for cat, t in rows]
    t0 = min(t for _, t in ts)
    by: dict[str, list[float]] = {}
    for cat, t in ts:
        by.setdefault(cat, []).append((t - t0).total_seconds())
    for cat, v in sorted(by.items(), key=lambda kv: max(kv[1])):
        v.sort()
        print(f"{cat:9} n={len(v):5}  first {v[0]:7.1f} s  median {v[len(v) // 2]:7.1f} s  last {v[-1]:7.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
