import csv
from typing import List

from .planner import MANIFEST_COLUMNS, Row


def write_manifest(rows: List[Row], path: str):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r.as_dict())


def read_manifest(path: str) -> List[Row]:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for d in csv.DictReader(f):
            d["source_size"] = int(d.get("source_size") or -1)
            out.append(Row(**{k: d.get(k, "") for k in MANIFEST_COLUMNS if k != "source_size"},
                           source_size=d["source_size"]))
    return out
