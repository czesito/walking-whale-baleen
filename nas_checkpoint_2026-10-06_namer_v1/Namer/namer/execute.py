"""執行、驗證、還原（N-EXEC、N-VERIFY、N-ROLLBACK；S-5～S-11）。
本模組沒有任何刪除檔案的程式碼路徑。"""
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import records
from .plan import RENAME, SKIP, Plan, plan
from .rules import Rules, key_of
from .scan import is_system_file, scan_fs

CONFIRM_PHRASE = "確認 rename"


class Aborted(Exception):
    pass


@dataclass
class ExecResult:
    run_dir: str
    planned: int
    done: int
    error: str = ""
    problems: List[str] = field(default_factory=list)
    verify_ok: bool = False
    replan_renames: int = -1


def _full(root, folder, name):
    return os.path.join(root, *folder, name)


def _same_file_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def rename_path(src: str, dst: str):
    """S-8：純大小寫改名用兩段式；失敗時改回。"""
    if _same_file_name(os.path.basename(src), os.path.basename(dst)) and src != dst:
        tmp = src + f".namer-tmp-{uuid.uuid4().hex[:8]}"
        os.rename(src, tmp)
        try:
            os.rename(tmp, dst)
        except OSError:
            os.rename(tmp, src)
            raise
    else:
        os.rename(src, dst)


def _exists_other(src: str, dst: str) -> bool:
    """目標是否被「別的」項目占用（純大小寫改名時，目標在不分大小寫的系統上就是自己）。"""
    if not os.path.exists(dst):
        return False
    if _same_file_name(os.path.basename(src), os.path.basename(dst)):
        try:
            return not os.path.samefile(src, dst)
        except OSError:
            return True
    return True


def countable_files(root: str, entries=None) -> int:
    entries = entries if entries is not None else scan_fs(root)
    return sum(1 for e in entries if not e.is_dir and not is_system_file(e.name))


def execute(root: str, approved: Plan, confirm: str, run_dir: Optional[str] = None,
            progress: Optional[Callable[[int, int, str], None]] = None) -> ExecResult:
    if confirm != CONFIRM_PHRASE:
        raise Aborted(f"未確認：必須輸入「{CONFIRM_PHRASE}」")
    rules: Rules = approved.rules
    # S-5：重新掃描、重新規劃、逐列比對
    entries = scan_fs(root)
    fresh = plan(entries, rules, root)
    if fresh.approval_key() != approved.approval_key():
        a, b = set(approved.approval_key()), set(fresh.approval_key())
        diff = [f"預覽有、現在沒有：{key_of(x[0] + (x[1],))} → {x[2]}" for x in sorted(a - b)][:10]
        diff += [f"現在多出：{key_of(x[0] + (x[1],))} → {x[2]}" for x in sorted(b - a)][:10]
        raise Aborted("資料夾在預覽之後有變動，未改任何檔案。請重新掃描預覽。\n" + "\n".join(diff))
    before_count = countable_files(root, entries)

    run_dir = run_dir or records.new_run_dir()
    with open(os.path.join(run_dir, "rules.json"), "w", encoding="utf-8") as f:
        f.write(rules.to_json())
    records.write_preview(approved, os.path.join(run_dir, "preview.csv"))
    log = records.RenameLog(os.path.join(run_dir, "rename_log.csv"))
    todo = approved.renames()
    res = ExecResult(run_dir, len(todo), 0)
    done: Dict[tuple, str] = {}
    try:
        for i, r in enumerate(todo, 1):
            src = _full(root, r.folder, r.name)
            dst = _full(root, r.folder, r.new_name)
            # S-6
            if r.is_dir:
                if not os.path.isdir(src):
                    raise Aborted(f"來源資料夾不存在：{src}")
            else:
                if not os.path.isfile(src):
                    raise Aborted(f"來源不存在：{src}")
                if os.path.getsize(src) != r.size:
                    raise Aborted(f"來源大小與預覽不同：{src}")
            if _exists_other(src, dst):
                raise Aborted(f"目標已存在：{dst}")
            rename_path(src, dst)
            log.write(r.folder, r.name, r.new_name, r.is_dir, r.size, "OK")      # S-7
            done[(r.folder, r.name)] = r.new_name
            res.done += 1
            if progress:
                progress(i, len(todo), r.new_name)
    except (Aborted, OSError) as e:
        res.error = str(e)
        if todo and res.done < len(todo):
            nr = todo[res.done]
            log.write(nr.folder, nr.name, nr.new_name, nr.is_dir, nr.size, f"ERROR: {e}")
    finally:
        log.close()

    records.write_name_map(approved, done, root, os.path.join(run_dir, "name_map.csv"))
    if not res.error:
        res.problems, res.replan_renames = verify(root, rules, os.path.join(run_dir, "rename_log.csv"), before_count)
        res.verify_ok = not res.problems
    _write_summary(res, approved, root)
    return res


def verify(root: str, rules: Rules, log_path: str, expected_count: Optional[int] = None):
    """N-VERIFY。回傳 (問題清單, 重新規劃後的 RENAME 數)。"""
    problems: List[str] = []
    ok_rows = [x for x in records.read_log(log_path) if x["status"] == "OK"]
    renamed_dirs = {}
    for x in ok_rows:
        if x["type"] == "dir":
            renamed_dirs[(x["folder"], x["old_name"])] = x["new_name"]
    for x in ok_rows:
        folder = tuple(p for p in x["folder"].split("\\") if p)
        new = _full(root, folder, x["new_name"])
        old = _full(root, folder, x["old_name"])
        if not os.path.exists(new):
            problems.append(f"新名稱不存在：{new}")
            continue
        if x["type"] == "file" and x["size"] and os.path.getsize(new) != int(x["size"]):
            problems.append(f"大小改變：{new}")
        if not _same_file_name(x["old_name"], x["new_name"]) and os.path.exists(old):
            problems.append(f"舊名稱仍存在：{old}")
    entries = scan_fs(root)
    if expected_count is not None:
        n = countable_files(root, entries)
        if n != expected_count:
            problems.append(f"檔案總數（不含系統檔）改變：執行前 {expected_count}，現在 {n}")
    again = plan(entries, rules, root)
    rn = again.counts().get(RENAME, 0)
    if rn:
        problems.append(f"重新規劃後仍有 {rn} 筆需要改名（應為 0）")
    return problems, rn


def rollback_preview(root: str, log_path: str):
    """N-ROLLBACK：回傳 (可還原列, 無法還原的說明)。"""
    rows = [x for x in records.read_log(log_path) if x["status"] == "OK"]
    can, cannot = [], []
    for x in reversed(rows):
        folder = tuple(p for p in x["folder"].split("\\") if p)
        cur, old = _full(root, folder, x["new_name"]), _full(root, folder, x["old_name"])
        if os.path.exists(cur) and not _exists_other(cur, old):
            can.append(x)
        else:
            cannot.append(f"狀態不符，略過：{cur}")
    # 先還原資料夾，再還原檔案
    can.sort(key=lambda x: x["type"] != "dir")
    return can, cannot


def rollback(root: str, log_path: str, confirm: str):
    if confirm != CONFIRM_PHRASE:
        raise Aborted(f"未確認：必須輸入「{CONFIRM_PHRASE}」")
    can, cannot = rollback_preview(root, log_path)
    rb = records.RenameLog(os.path.join(os.path.dirname(log_path), "rollback_log.csv"))
    done = 0
    try:
        for x in can:
            folder = tuple(p for p in x["folder"].split("\\") if p)
            cur, old = _full(root, folder, x["new_name"]), _full(root, folder, x["old_name"])
            if not os.path.exists(cur) or _exists_other(cur, old):
                cannot.append(f"狀態已改變，略過：{cur}")
                continue
            rename_path(cur, old)
            rb.write(folder, x["new_name"], x["old_name"], x["type"] == "dir", x["size"], "OK")
            done += 1
    finally:
        rb.close()
    return done, cannot


def _write_summary(res: ExecResult, approved: Plan, root: str):
    c = approved.counts()
    lines = [
        "Namer 執行摘要",
        f"根資料夾：{root}",
        f"前綴：{approved.rules.prefix}",
        f"預覽：RENAME {c.get('RENAME', 0)} / KEEP {c.get('KEEP', 0)} / REVIEW {c.get('REVIEW', 0)} / SKIP {c.get('SKIP', 0)}",
        f"已改名：{res.done} / {res.planned}",
        f"錯誤：{res.error or '無'}",
        f"驗證：{'通過' if res.verify_ok else ('未執行' if res.error else '有問題')}",
    ]
    lines += [f"  - {p}" for p in res.problems]
    reviews = [r for r in approved.rows if r.action == "REVIEW"]
    lines.append(f"\nREVIEW（未改名）{len(reviews)} 筆：")
    why = Counter(r.reason.split(":")[0] for r in reviews)
    lines += [f"  {k}: {v}" for k, v in why.most_common()]
    lines += [f"  {key_of(r.folder + (r.name,))}  ← {r.reason}" for r in reviews[:500]]
    with open(os.path.join(res.run_dir, "summary.txt"), "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
