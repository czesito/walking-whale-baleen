"""Jinja2 environment for src/baleen/templates (design handoff §12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import present
from .views import ordered_codes

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def qs(**params: Any) -> str:
    """Query string without empty values: qs(status='review', q='', page=1) -> 'status=review'."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        if k == "page" and int(v) <= 1:
            continue
        out[k] = v
    return urlencode(out)


def make_env(version: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "svg")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        static=lambda name: f"/static/{name}?v={version}",
        qs=qs,
        num=present.num,
        size=present.size,
        when=present.when,
        clock_time=present.clock_time,
        local_stamp=present.local_stamp,
        duration=present.duration,
        split_path=present.split_path,
        status_key=present.status_key,
        reason_label=present.reason_label,
        ordered_codes=ordered_codes,
        pct_class=present.pct_class,
        KEY_LABEL=present.KEY_LABEL,
        STATUS_KEYS=present.STATUS_KEYS,
        CATEGORY_LABEL=present.CATEGORY_LABEL,
        version=version,
        version_short=".".join(version.split(".")[:2]),
    )
    return env
