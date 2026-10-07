"""紀錄與設定檔（N-LOG、§8 規則檔、S-9）。"""
import csv
import glob
import hashlib
import os
from datetime import datetime
from typing import Dict, List, Optional

from .plan import Plan, Row
from .rules import Rules, key_of, suggest_rules

PREVIEW_COLS = ["folder", "name", "type", "size", "action", "new_name", "reason",
                "group", "seq", "origin", "date_source", "note"]


def app_home() -> str:
    return os.environ.get("NAMER_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir(*parts) -> str:
    p = os.path.join(app_home(), "data", *parts)
    os.makedirs(p, exist_ok=True)
    return p


def new_run_dir() -> str:
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs = data_dir("runs")
    name, n = base, 2
    while os.path.exists(os.path.join(runs, name)):
        name = f"{base}-{n}"
        n += 1
    p = os.path.join(runs, name)
    os.makedirs(p)
    return p


def latest_run_with_log() -> Optional[str]:
    runs = sorted(glob.glob(os.path.join(data_dir("runs"), "*", "rename_log.csv")))
    return os.path.dirname(runs[-1]) if runs else None


# ---------- 規則檔（每個根資料夾一份）----------
def _profile_path(root: str) -> str:
    h = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(data_dir("profiles"), h + ".json")


def load_rules_for(root: str) -> Rules:
    p = _profile_path(root)
    if os.path.exists(p):
        try:
            return Rules.from_json(open(p, encoding="utf-8").read())
        except Exception:  # noqa — 壞掉的規則檔：用建議值，不中斷
            pass
    return suggest_rules(os.path.basename(os.path.normpath(root)))


def save_rules_for(root: str, rules: Rules):
    with open(_profile_path(root), "w", encoding="utf-8") as f:
        f.write(rules.to_json())


# ---------- CSV ----------
def row_dict(r: Row) -> Dict:
    return {"folder": key_of(r.folder), "name": r.name, "type": "dir" if r.is_dir else "file",
            "size": "" if r.is_dir else r.size, "action": r.action, "new_name": r.new_name,
            "reason": r.reason, "group": r.group, "seq": r.seq, "origin": r.origin,
            "date_source": r.date_source, "note": r.note}


def write_preview(plan: Plan, path: str):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PREVIEW_COLS)
        w.writeheader()
        for r in plan.rows:
            w.writerow(row_dict(r))


class RenameLog:
    COLS = ["time", "folder", "old_name", "new_name", "type", "size", "status"]

    def __init__(self, path: str):
        self.path = path
        new = not os.path.exists(path)
        self.f = open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=self.COLS)
        if new:
            self.w.writeheader()
            self._sync()

    def write(self, folder, old, new, is_dir, size, status):
        self.w.writerow({"time": datetime.now().isoformat(timespec="seconds"), "folder": key_of(folder),
                         "old_name": old, "new_name": new, "type": "dir" if is_dir else "file",
                         "size": "" if is_dir else size, "status": status})
        self._sync()

    def _sync(self):
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass

    def close(self):
        self.f.close()


def read_log(path: str) -> List[Dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------- Baleen 出處（N-LOG name_map）----------
def baleen_sources(root: str) -> Dict[str, str]:
    """output_path（相對、/ 分隔，casefold）→ Baleen 的 source_path。沒有報告回傳空 dict。"""
    reps = sorted(glob.glob(os.path.join(root, "_baleen", "report-*.csv")))
    out: Dict[str, str] = {}
    for rep in reps:                      # 新的覆蓋舊的
        try:
            with open(rep, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    op = (row.get("output_path") or "").strip()
                    if op:
                        out[op.replace("\\", "/").casefold()] = row.get("source_path", "")
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return out


def write_name_map(plan: Plan, done: Dict[tuple, str], root: str, path: str):
    """執行後所有檔案的最終名稱。done：(folder, old_name) → new_name（實際改名成功的）。"""
    src = baleen_sources(root)
    cols = ["final_path", "previous_name", "action", "reason", "baleen_source_path"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in plan.rows:
            if r.is_dir:
                continue
            final = done.get((r.folder, r.name), r.name)
            # 附件資料夾若改名，檔案路徑跟著變
            folder = tuple(done.get((r.folder[:i], r.folder[i]), r.folder[i]) for i in range(len(r.folder)))
            old_rel = "/".join(r.folder + (r.name,))
            w.writerow({"final_path": key_of(folder + (final,)),
                        "previous_name": r.name if final != r.name else "",
                        "action": "RENAMED" if final != r.name else r.action, "reason": r.reason,
                        "baleen_source_path": src.get(old_rel.casefold(), "")})
