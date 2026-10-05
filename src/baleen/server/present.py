"""Presentation helpers: the formats of design §10 (numbers, dates, durations, sizes, paths)
and the status vocabulary of design §03 / D-13."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from ..model import REASONS, STATUS_KEY, STATUS_LABEL, STATUS_ORDER, Status, split_reasons

# Status keys in tile / sort order (UI-R3): failed, review, unsupported, ok, ignored, skipped.
STATUS_KEYS: tuple[str, ...] = tuple(STATUS_KEY[s] for s in STATUS_ORDER)
KEY_TO_STATUS: dict[str, Status] = {v: k for k, v in STATUS_KEY.items()}
KEY_LABEL: dict[str, str] = {STATUS_KEY[s]: STATUS_LABEL[s] for s in STATUS_ORDER} | {"running": "Running"}

CATEGORY_LABEL: dict[str, str] = {
    "image": "Images", "video": "Video", "document": "Documents", "email": "E-mail", "pdf": "PDF",
    "audio": "Audio", "text": "Text", "html": "Web pages", "other": "Other",
}

CHECK_STATE = {
    "pass": ("st-ok", "pass", "pass"),
    "fail": ("st-failed", "fail", "fail"),
    "unavailable": ("st-unavail", "unav", "unavailable"),
    "n/a": ("st-skipped", "na", "n/a"),
}


def num(n: Any) -> str:
    """Thousands separators: 1,670."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return ""


def size(b: int | None) -> str:
    """Decimal units: 980 KB · 4.82 MB · 1.2 GB."""
    if b is None:
        return "—"
    b = int(b)
    if b < 1000:
        return f"{b} B"
    if b < 1_000_000:
        return f"{b / 1000:.0f} KB"
    if b < 1_000_000_000:
        mb = b / 1_000_000
        return f"{mb:.2f} MB" if mb < 10 else f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"
    gb = b / 1_000_000_000
    if gb < 1000:
        return f"{gb:.1f} GB"
    return f"{gb / 1000:.1f} TB"


def gb_free(b: int) -> str:
    """Free space in whole decimal GB: '212 GB free'."""
    return f"{b / 1_000_000_000:,.0f} GB"


def parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def when(iso: str | None, now: datetime | None = None) -> str:
    """Today 14:30 · Yesterday 16:22 · 2 Oct 09:30 · 2 Oct 2025 09:30 (24-hour clock)."""
    dt = parse_iso(iso)
    if dt is None:
        return "—"
    local = dt.astimezone()
    now = (now or datetime.now(UTC)).astimezone()
    hm = local.strftime("%H:%M")
    if local.date() == now.date():
        return f"Today {hm}"
    if local.date() == (now - timedelta(days=1)).date():
        return f"Yesterday {hm}"
    if local.year == now.year:
        return f"{local.day} {local.strftime('%b')} {hm}"
    return f"{local.day} {local.strftime('%b')} {local.year} {hm}"


def clock_time(iso: str | None) -> str:
    dt = parse_iso(iso)
    return dt.astimezone().strftime("%H:%M") if dt else "—"


def local_stamp(iso: str | None) -> str:
    """2004-05-30 22:41 (inspector 'Modified')."""
    dt = parse_iso(iso)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def duration(seconds: float | None) -> str:
    """18m 42s · 1h 05m · 0m 20s."""
    if seconds is None:
        return "—"
    s = max(0, int(round(seconds)))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s"


def elapsed(seconds: float) -> str:
    """Live elapsed time: 09:21, or 1:05:12 past an hour."""
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def span(started: str | None, finished: str | None) -> float | None:
    a, b = parse_iso(started), parse_iso(finished)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def split_path(p: str | None) -> tuple[str, str]:
    """('2004-07 Poetry evening/', 'notes.txt'): the folder truncates, the name stays visible (D-15)."""
    if not p:
        return "", ""
    i = max(p.rfind("/"), p.rfind("\\"))  # report paths use '/', Windows roots use '\'
    return ("", p) if i < 0 else (p[: i + 1], p[i + 1:])


def status_key(status: str | Status | None) -> str:
    if status is None:
        return "ok"
    if isinstance(status, Status):
        return STATUS_KEY[status]
    try:
        return STATUS_KEY[Status(status)]
    except ValueError:
        return status if status in KEY_LABEL else "ok"


def codes(reason: str) -> list[str]:
    return split_reasons(reason)


def reason_label(code: str) -> str:
    r = REASONS.get(code)
    return r.label if r else code


def reason_help(code: str) -> str:
    r = REASONS.get(code)
    return r.help if r else ""


def is_note(code: str) -> bool:
    r = REASONS.get(code)
    return bool(r and r.is_note)


def file_manager() -> str:
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform == "win32":
        return "File Explorer"
    return "the file manager"


def plural(n: int, one: str, many: str) -> str:
    return f"{num(n)} {one if n == 1 else many}"


def pct_class(value: float) -> str:
    """Width utility class for bars (no inline styles under the SEC-6 CSP): w-0 … w-100."""
    v = max(0, min(100, int(round(value))))
    return f"w-{v}"


def short_home(path: str, home: str) -> str:
    """data/logs/baleen.log rather than the absolute path, when it lies inside BALEEN_HOME."""
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")
