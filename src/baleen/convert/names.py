"""Sanitising attachment names (spec §7.8). Applies only to names from inside e-mails; source
names are never changed.

1. Strip path components and control characters.
2. Replace < > : " / \\ | ? * with _.
3. Trim trailing dots and spaces.
4. Suffix Windows reserved names (CON PRN AUX NUL COM1-9 LPT1-9) with _.
5. Truncate the stem so the name is <= 200 bytes in UTF-8.
6. An empty result -> attachment-<n> (plus the extension from the MIME type, as for an
   attachment without a filename, §6.5).
"""

from __future__ import annotations

import re
import unicodedata

MAX_NAME_BYTES = 200
_BAD = set('<>:"/\\|?*')
RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                      *(f"LPT{i}" for i in range(1, 10))})


def _split(name: str) -> tuple[str, str]:
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    return name[:i], name[i:]


def _truncate(name: str, limit: int = MAX_NAME_BYTES) -> str:
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, ext = _split(name)
    if len(ext.encode("utf-8")) > limit // 4:  # a pathological "extension": cut the whole name
        stem, ext = name, ""
    budget = limit - len(ext.encode("utf-8"))
    out: list[str] = []
    used = 0
    for ch in stem:
        b = len(ch.encode("utf-8"))
        if used + b > budget:
            break
        out.append(ch)
        used += b
    return "".join(out) + ext


def sanitise_attachment_name(name: str | None, n: int, ext: str = "") -> str:
    """Return the §7.8 name for an attachment. `n` is its 1-based order in the e-mail; `ext` the
    extension implied by its MIME type (used only when nothing usable is left)."""
    s = name or ""
    # lone surrogates (undecodable bytes) can't be file names
    s = "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in s)
    s = re.split(r"[/\\]", s)[-1]
    s = "".join(c for c in s if unicodedata.category(c) != "Cc")
    s = "".join("_" if c in _BAD else c for c in s)
    s = s.rstrip(". ")
    if s:
        base = s.split(".", 1)[0]
        if base.rstrip(" ").upper() in RESERVED:
            s = base + "_" + s[len(base):]
    s = _truncate(s).rstrip(". ")
    if not s:
        s = f"attachment-{n}{ext}"
    return s
