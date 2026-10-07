"""規則與使用者覆寫（NAMING_SPEC §4、§8）。"""
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

PREFIX_RE = re.compile(r"^[A-Za-z0-9]+(_[A-Za-z0-9]+)*_$")
CODE_RE = re.compile(r"^[0-9A-Za-z]+$")
DEFAULT_ARCHIVAL = [".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".mp4", ".m4a"]
DEFAULT_BASE = "DP2_99_"


def key_of(parts) -> str:
    """資料夾相對路徑 → 規則檔用的鍵（Windows 分隔符號）。"""
    return "\\".join(parts)


def parts_of(key: str) -> tuple:
    return tuple(p for p in key.replace("/", "\\").split("\\") if p)


@dataclass
class Override:
    code: str = ""        # 指定群組代碼（空字串 = 自動）
    share: bool = False   # 本資料夾與所有子資料夾共用一個池（N-SHARE）
    exclude: bool = False  # 不處理（含子資料夾）

    def is_default(self) -> bool:
        return not (self.code or self.share or self.exclude)

    def label(self) -> str:
        if self.exclude:
            return "排除"
        bits = []
        if self.share:
            bits.append("子資料夾共用")
        if self.code:
            bits.append(f"代碼 {self.code}")
        return "、".join(bits)


@dataclass
class Rules:
    prefix: str = "DP2_99_00_"
    year_from: int = 1900
    year_to: int = 2099
    attachments: str = "follow"          # follow | keep（N-ATT）
    archival_exts: List[str] = field(default_factory=lambda: list(DEFAULT_ARCHIVAL))
    overrides: Dict[str, Override] = field(default_factory=dict)

    def validate(self) -> List[str]:
        errs = []
        if not PREFIX_RE.match(self.prefix or ""):
            errs.append(f"前綴格式不符（例：DP2_99_04_）：{self.prefix!r}")
        if not (1000 <= self.year_from <= self.year_to <= 9999):
            errs.append(f"年份範圍不合理：{self.year_from}–{self.year_to}")
        if self.attachments not in ("follow", "keep"):
            errs.append(f"attachments 只能是 follow 或 keep：{self.attachments!r}")
        for k, o in self.overrides.items():
            if o.code and not CODE_RE.match(o.code):
                errs.append(f"資料夾「{k}」的代碼只能是英數字：{o.code!r}")
        return errs

    def override(self, parts) -> Optional[Override]:
        o = self.overrides.get(key_of(parts))
        return o if o and not o.is_default() else None

    def to_json(self) -> str:
        d = asdict(self)
        d["overrides"] = {k: asdict(v) for k, v in sorted(self.overrides.items()) if not v.is_default()}
        return json.dumps(d, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Rules":
        d = json.loads(text)
        ov = {k: Override(**v) for k, v in (d.pop("overrides", None) or {}).items()}
        r = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        r.overrides = ov
        r.archival_exts = [e.lower() for e in r.archival_exts]
        return r


def suggest_rules(root_name: str, base: str = DEFAULT_BASE) -> Rules:
    """從根資料夾名稱推測前綴與年份範圍（UI-1）。"""
    r = Rules()
    m = re.match(r"^\s*(\d+)", root_name)
    r.prefix = f"{base}{m.group(1)}_" if m else f"{base}00_"
    y = re.search(r"\(\s*(\d{4})\s*[-–~]\s*(\d{4})\s*\)", root_name)
    if y and int(y.group(1)) <= int(y.group(2)):
        r.year_from, r.year_to = int(y.group(1)), int(y.group(2))
    return r
