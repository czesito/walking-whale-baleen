"""規劃（純函式，不碰檔案系統）。NAMING_SPEC §4–§10。"""
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .folderdate import FolderDate, folder_sort_key, nfc_fold, parse_folder_name
from .rules import Override, Rules, key_of
from .scan import Entry, is_system_file

RENAME, KEEP, REVIEW, SKIP = "RENAME", "KEEP", "REVIEW", "SKIP"
ACTIONS = (RENAME, KEEP, REVIEW, SKIP)
LETTERS = "abcdefghijklmnopqrstuvwxyz"
ATT_SUFFIX = "_attachments"
EXT_NORMAL = {".jpeg": ".jpg", ".tiff": ".tif"}
LETTER_CODE_RE = re.compile(r"^(\d{6})([a-z]+)$")
MAX_PATH = 250


@dataclass
class Row:
    folder: tuple
    name: str
    is_dir: bool = False
    size: int = -1
    action: str = ""
    new_name: str = ""
    reason: str = ""
    group: str = ""
    seq: str = ""
    origin: str = ""          # EXISTING | ASSIGNED | OVERRIDE | CONTINUE
    date_source: str = ""
    note: str = ""

    @property
    def folder_key(self) -> str:
        return key_of(self.folder)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1]

    def set(self, action, reason="", new_name=None):
        self.action, self.reason = action, reason
        if new_name is not None:
            self.new_name = new_name    # REVIEW 列保留建議名稱，僅供參考，不會執行


@dataclass
class UnitInfo:
    folder: tuple
    group: str = ""
    date_source: str = ""
    pool: str = ""
    override: str = ""
    counts: Counter = field(default_factory=Counter)
    reason: str = ""


@dataclass
class Plan:
    root: str
    rules: Rules
    rows: List[Row]
    units: List[UnitInfo]

    def counts(self) -> Counter:
        return Counter(r.action for r in self.rows)

    def renames(self) -> List[Row]:
        files = [r for r in self.rows if r.action == RENAME and not r.is_dir]
        dirs = [r for r in self.rows if r.action == RENAME and r.is_dir]
        key = lambda r: (nfc_fold(r.folder_key), r.folder_key, nfc_fold(r.name), r.name)
        return sorted(files, key=key) + sorted(dirs, key=key)

    def approval_key(self) -> List[Tuple]:
        """S-5：核准比對用的鍵。"""
        return [(r.folder, r.name, r.new_name, r.is_dir, r.size if not r.is_dir else -1) for r in self.renames()]


def norm_ext(ext: str) -> str:
    e = ext.lower()
    return EXT_NORMAL.get(e, e)


def file_sort_key(r: Row):
    return (nfc_fold(r.stem), nfc_fold(r.ext), r.stem, r.ext)


def path_sort_key(parts: tuple):
    k = key_of(parts)
    return (nfc_fold(k), k)


def in_attachments(folder: tuple) -> bool:
    return any(p.casefold().endswith(ATT_SUFFIX) for p in folder)


def _resolve_date(parts: tuple, rules: Rules):
    """N-DATE + N-INHERIT：回傳 (FolderDate, 日期來源 parts, 是否繼承)。"""
    if not parts:
        return FolderDate(False, reason="ROOT"), (), False
    own = parse_folder_name(parts[-1], rules.year_from, rules.year_to)
    if own.ok:
        return own, parts, False
    for i in range(len(parts) - 1, 0, -1):
        c = parse_folder_name(parts[i - 1], rules.year_from, rules.year_to)
        if c.ok:
            return c, parts[:i], True
    return own, (), False


@dataclass
class _Pool:
    pid: str
    anchor: tuple                 # 單位本身，或 share 的資料夾
    units: List[tuple]
    fd: FolderDate = None
    date_src: tuple = ()
    inherited: bool = False
    override_code: str = ""
    code: str = ""
    origin: str = ""              # 群組代碼來源：EXISTING / OVERRIDE / ASSIGNED
    review: str = ""


def plan(entries: List[Entry], rules: Rules, root: str = "") -> Plan:
    errs = rules.validate()
    if errs:
        raise ValueError("；".join(errs))
    prefix = rules.prefix
    exist_re = re.compile(r"^" + re.escape(prefix) + r"([0-9A-Za-z]+)_(\d{2,})$")
    archival = {e.lower() for e in rules.archival_exts}

    files = [e for e in entries if not e.is_dir]
    dirs = [e for e in entries if e.is_dir]
    rows: List[Row] = []
    by_unit: Dict[tuple, List[Row]] = defaultdict(list)
    excluded = [parts for k, o in rules.overrides.items() if o.exclude for parts in [tuple(k.split("\\"))]]

    def is_excluded(folder):
        return any(folder[:len(p)] == p for p in excluded)

    # ---- 逐檔初步分類 ----
    for e in files:
        r = Row(e.folder, e.name, False, e.size)
        rows.append(r)
        if is_system_file(e.name):
            r.set(SKIP, "SYSTEM_FILE")
        elif in_attachments(e.folder):
            r.set(SKIP, "ATTACHMENT_CONTENT")
        elif not e.folder:
            r.set(REVIEW, "FILE_DIRECTLY_UNDER_ROOT")
        elif is_excluded(e.folder):
            r.set(SKIP, "EXCLUDED_BY_USER")
        else:
            by_unit[e.folder].append(r)

    # ---- 分配池（N-POOL）----
    share_roots = sorted((tuple(k.split("\\")) for k, o in rules.overrides.items()
                          if o.share and not o.exclude), key=len)

    def share_of(unit):
        for s in share_roots:              # 最外層優先
            if unit[:len(s)] == s:
                return s
        return None

    pools: Dict[str, _Pool] = {}
    pool_of: Dict[tuple, str] = {}
    for unit in sorted(by_unit, key=path_sort_key):
        s = share_of(unit)
        anchor = s if s is not None else unit
        pid = ("S:" if s is not None else "U:") + key_of(anchor)
        if pid not in pools:
            fd, src, inh = _resolve_date(anchor, rules)
            o = rules.override(anchor)
            pools[pid] = _Pool(pid, anchor, [], fd, src, inh, o.code if o else "")
        pools[pid].units.append(unit)
        pool_of[unit] = pid

    def pool_rows(p: _Pool) -> List[Row]:
        return [r for u in p.units for r in by_unit[u]]

    # 既有命名（N-EXIST）
    existing: Dict[int, Tuple[str, int]] = {}
    for r in rows:
        if r.action:
            continue
        m = exist_re.match(r.stem)
        if m:
            existing[id(r)] = (m.group(1), int(m.group(2)))

    # 字母占用表：所有既有與指定代碼
    occupied: Dict[str, Set[str]] = defaultdict(set)

    def claim(code):
        m = LETTER_CODE_RE.match(code)
        if m:
            occupied[m.group(1)].add(m.group(2))

    for code, _ in existing.values():
        claim(code)
    for o in rules.overrides.values():
        if o.code and not o.exclude:
            claim(o.code)

    # ---- N-GROUP ----
    for p in pools.values():
        codes = {existing[id(r)][0] for r in pool_rows(p) if id(r) in existing}
        if p.override_code:
            other = codes - {p.override_code}
            if other:
                p.review = "EXISTING_CODE_CONFLICTS_OVERRIDE:" + ",".join(sorted(other))
            else:
                p.code, p.origin = p.override_code, "OVERRIDE"
        elif len(codes) > 1:
            p.review = "EXISTING_MULTIPLE_CODES:" + ",".join(sorted(codes))
        elif codes:
            c = next(iter(codes))
            m = LETTER_CODE_RE.match(c)
            if m and p.fd.ok and m.group(1) != p.fd.yyyymm:
                p.review = f"EXISTING_CODE_DATE_MISMATCH(folder={p.fd.yyyymm},name={c})"
            else:
                p.code, p.origin = c, "EXISTING"
        elif not p.fd.ok:
            p.review = f"DATE_NOT_DETERMINABLE:{p.fd.reason}"

    # ---- 同代碼合池（N-SIBLING）----
    by_code: Dict[str, List[_Pool]] = defaultdict(list)
    for p in pools.values():
        if p.code and not p.review:
            by_code[p.code].append(p)
    for code, ps in by_code.items():
        if len(ps) < 2:
            continue
        parents = {p.anchor[:-1] for p in ps}
        if len(parents) > 1 or parents == {()}:      # 根資料夾底下的兄弟不算（N-SIBLING）
            for p in ps:
                p.review = f"CODE_SHARED_BY_UNRELATED_FOLDERS:{code}"
            continue
        ps.sort(key=lambda p: path_sort_key(p.anchor))
        head = ps[0]
        for p in ps[1:]:
            head.units.extend(p.units)
            for u in p.units:
                pool_of[u] = head.pid
            if p.origin == "OVERRIDE":
                head.origin = "OVERRIDE"
            del pools[p.pid]
        head.units.sort(key=path_sort_key)

    # ---- N-LETTER ----
    need: Dict[str, List[_Pool]] = defaultdict(list)
    for p in pools.values():
        if not p.code and not p.review:
            need[p.fd.yyyymm].append(p)
    for ym, ps in need.items():
        ps.sort(key=lambda p: folder_sort_key(
            p.fd, key_of(p.anchor) if p.inherited else p.anchor[-1], key_of(p.anchor)))
        for p in ps:
            for le in LETTERS:
                if le not in occupied[ym]:
                    occupied[ym].add(le)
                    p.code, p.origin = ym + le, "ASSIGNED"
                    break
            else:
                p.review = "LETTER_OVERFLOW"

    # ---- N-SEQ 與檔名 ----
    for p in pools.values():
        prs = pool_rows(p)
        dsrc = key_of(p.date_src)
        for r in prs:
            r.date_source = dsrc
        if p.review:
            for r in prs:
                if not r.action:
                    r.set(REVIEW, p.review)
            continue
        r_code = p.code
        used: Counter = Counter()
        for r in prs:
            if id(r) in existing and existing[id(r)][0] == r_code:
                used[existing[id(r)][1]] += 1
        dup = {n for n, c in used.items() if c > 1}
        taken = set(used)
        nxt = 1
        ordered = []
        for u in p.units:
            ordered.extend(sorted(by_unit[u], key=file_sort_key))
        for r in ordered:
            r.group = r_code
            ext_ok = norm_ext(r.ext) in {norm_ext(e) for e in archival}
            if id(r) in existing:
                n = existing[id(r)][1]
                r.seq = f"{n:02d}"
                r.origin = "EXISTING"
                if n in dup:
                    r.set(REVIEW, f"DUPLICATE_EXISTING_SEQ:{r_code}_{n:02d}")
                elif not ext_ok:
                    r.set(REVIEW, "NOT_ARCHIVAL_FORMAT（請先用 Baleen 轉檔）")
                else:
                    new = r.stem + norm_ext(r.ext)
                    r.set(KEEP if new == r.name else RENAME,
                          "EXISTING_NAME" if new == r.name else "EXTENSION_NORMALISED", new)
                continue
            if not ext_ok:
                r.set(REVIEW, "NOT_ARCHIVAL_FORMAT（請先用 Baleen 轉檔）")
                continue
            while nxt in taken:
                nxt += 1
            taken.add(nxt)
            r.seq = f"{nxt:02d}"
            r.origin = {"EXISTING": "CONTINUE", "OVERRIDE": "OVERRIDE"}.get(p.origin, "ASSIGNED")
            new = f"{prefix}{r_code}_{r.seq}{norm_ext(r.ext)}"
            r.set(KEEP if new == r.name else RENAME,
                  {"CONTINUE": "CONTINUE_EXISTING_GROUP", "OVERRIDE": "GROUP_FROM_OVERRIDE"}.get(r.origin, "ASSIGNED"),
                  new)
            if p.inherited:
                r.note = f"日期繼承自 {dsrc}"

    # ---- N-CHECK（檔案）----
    dir_names: Dict[tuple, List[str]] = defaultdict(list)
    for d in dirs:
        dir_names[d.folder].append(d.name)
    _check(rows, dir_names, root, att_rows=[])

    # ---- N-ATT ----
    att_rows: List[Row] = []
    by_folder_name = {(r.folder, r.name.casefold()): r for r in rows}
    for d in dirs:
        if not d.name.casefold().endswith(ATT_SUFFIX) or in_attachments(d.folder) or not d.folder:
            continue
        if is_excluded(d.folder):
            continue
        r = Row(d.folder, d.name, True)
        att_rows.append(r)
        if rules.attachments == "keep":
            r.set(SKIP, "ATTACHMENT_FOLDER_KEPT")
            continue
        stem = d.name[:-len(ATT_SUFFIX)]
        mail = by_folder_name.get((d.folder, (stem + ".pdf").casefold()))
        if mail is None:
            r.set(REVIEW, "ATTACHMENT_PARENT_NOT_FOUND")
        elif mail.action == REVIEW:
            r.set(REVIEW, f"ATTACHMENT_PARENT_REVIEW:{mail.name}")
        elif mail.action == RENAME:
            new = os.path.splitext(mail.new_name)[0] + ATT_SUFFIX
            r.set(RENAME, f"FOLLOWS_MAIL_PDF:{mail.name}", new)
            r.group, r.seq = mail.group, mail.seq
        else:
            r.set(KEEP, f"FOLLOWS_MAIL_PDF:{mail.name}")
    rows.extend(att_rows)
    if att_rows:
        _check(rows, dir_names, root, att_rows)

    # ---- 單位摘要（UI-3）----
    units = []
    for u in sorted(by_unit, key=path_sort_key):
        p = pools[pool_of[u]]
        o = rules.override(u)
        ui = UnitInfo(u, p.code if not p.review else "", key_of(p.date_src),
                      key_of(p.anchor) if p.anchor != u else "", o.label() if o else "",
                      Counter(r.action for r in by_unit[u]), p.review)
        units.append(ui)
    # 只有覆寫、沒有直接檔案的資料夾（例：share 的父資料夾）也列出來，方便 UI 編輯
    listed = {u.folder for u in units}
    anchored = {p.anchor: p for p in pools.values()}
    for k, o in rules.overrides.items():
        parts = tuple(k.split("\\"))
        if not o.is_default() and parts not in listed:
            p = anchored.get(parts)
            units.append(UnitInfo(parts, (p.code if p and not p.review else ""), key_of(p.date_src) if p else "",
                                  "", o.label(), Counter(), (p.review if p else "")))
    units.sort(key=lambda u: path_sort_key(u.folder))

    rows.sort(key=lambda r: (path_sort_key(r.folder), r.is_dir, nfc_fold(r.name), r.name))
    return Plan(root, rules, rows, units)


def _check(rows: List[Row], dir_names, root: str, att_rows):
    """N-CHECK：重複目標、目標被占用、改名鏈、路徑長度。反覆執行直到穩定。"""
    root_len = len(root.rstrip("\\/")) if root else 0
    changed = True
    while changed:
        changed = False
        by_folder: Dict[tuple, List[Row]] = defaultdict(list)
        for r in rows:
            by_folder[r.folder].append(r)
        for folder, rs in by_folder.items():
            renames = [r for r in rs if r.action == RENAME]
            if not renames:
                continue
            ren_dirs = {r.name.casefold() for r in rs if r.is_dir and r.action == RENAME}
            stays = {r.name.casefold() for r in rs if r.action != RENAME}
            stays |= {n.casefold() for n in dir_names.get(folder, []) if n.casefold() not in ren_dirs}
            sources = {r.name.casefold() for r in renames}
            tcount = Counter(r.new_name.casefold() for r in renames)
            for r in renames:
                t = r.new_name.casefold()
                full_len = root_len + 1 + len(key_of(folder)) + 1 + len(r.new_name)
                if tcount[t] > 1:
                    r.set(REVIEW, f"DUPLICATE_TARGET:{r.new_name}")
                elif t in stays:
                    r.set(REVIEW, f"TARGET_OCCUPIED:{r.new_name}")
                elif t in sources and t != r.name.casefold():
                    r.set(REVIEW, f"TARGET_IS_ANOTHER_SOURCE:{r.new_name}")
                elif root and full_len >= MAX_PATH:
                    r.set(REVIEW, f"PATH_TOO_LONG:{full_len}")
                else:
                    continue
                changed = True
