"""Settings schema, defaults, load/save and validation (spec §11).

Layout of data/settings.json:

    {
      "schema_version": 1,
      "workflow": { source_dir, output_dir, check_dir, copy_existing, pdfa_level, ... },
      "app":      { processor_use, processor_cores, memory_limit, ... },
      "advanced": { lo_timeout_s, verapdf_timeout_s, av_timeout_min_s }
    }

An unreadable or invalid file gives defaults plus a visible warning. Invalid single values
fall back to their default, also with a warning.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Field:
    key: str
    kind: str  # "bool" | "enum" | "int" | "path" | "str"
    default: Any
    values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    def coerce(self, raw: Any) -> Any:
        """Convert a raw value (JSON, form or CLI string) into the field type; ValueError if invalid."""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("true", "1", "on", "yes"):
                return True
            if isinstance(raw, str) and raw.strip().lower() in ("false", "0", "off", "no"):
                return False
            raise ValueError(f"{self.key}: expected true or false")
        if self.kind == "enum":
            v = str(raw).strip()
            if v not in self.values:
                raise ValueError(f"{self.key}: expected one of {', '.join(self.values)}")
            return v
        if self.kind == "int":
            if isinstance(raw, bool):
                raise ValueError(f"{self.key}: expected a whole number")
            try:
                v = int(str(raw).strip())
            except ValueError as e:
                raise ValueError(f"{self.key}: expected a whole number") from e
            if self.minimum is not None and v < self.minimum:
                raise ValueError(f"{self.key}: at least {self.minimum}")
            if self.maximum is not None and v > self.maximum:
                raise ValueError(f"{self.key}: at most {self.maximum}")
            return v
        if self.kind == "path":
            v = "" if raw is None else str(raw)
            if v and not os.path.isabs(v):
                raise ValueError(f"{self.key}: must be an absolute path")
            return v
        return "" if raw is None else str(raw)


# Workflow settings (§11 first table). check_dir is the Check workflow's last-used folder.
WORKFLOW_FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        Field("source_dir", "path", ""),
        Field("output_dir", "path", ""),
        Field("check_dir", "path", ""),
        Field("copy_existing", "bool", True),
        Field("pdfa_level", "enum", "2b", ("1b", "2b", "3b")),
        Field("txt_encoding", "enum", "auto", ("auto", "big5", "gb18030", "shift_jis", "windows-1252")),
        Field("eml_attachments", "enum", "extract", ("extract", "block", "list")),
        Field("hide_email_addresses", "bool", False),
        Field("video_quality", "enum", "high", ("high", "standard")),
        Field("audio_container", "enum", "m4a", ("m4a", "mp4")),
    )
}
FOLDER_KEYS = ("source_dir", "output_dir", "check_dir")

# App preferences (§11 second table). Upper bounds that depend on the machine (cores, RAM)
# are clamped when budgets are resolved (scheduler.resolve_budget).
APP_FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        Field("processor_use", "enum", "balanced", ("gentle", "balanced", "maximum", "custom")),
        Field("processor_cores", "int", 4, minimum=1, maximum=4096),
        Field("memory_limit", "enum", "auto", ("auto", "custom")),
        Field("memory_gb", "int", 4, minimum=1, maximum=1 << 20),
        Field("transfer_slots", "enum", "auto", ("auto", "custom")),
        Field("transfer_count", "int", 4, minimum=1, maximum=16),
        Field("low_priority", "bool", True),
        Field("keep_awake", "bool", True),
        Field("work_dir", "str", "data/work"),
        Field("notify_on_finish", "bool", False),
        Field("progress_in_title", "bool", True),
    )
}
RESOURCE_KEYS = (
    "processor_use", "processor_cores", "memory_limit", "memory_gb",
    "transfer_slots", "transfer_count", "low_priority", "keep_awake", "work_dir",
)

# Advanced (file only, not in the UI).
ADVANCED_FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        Field("lo_timeout_s", "int", 300, minimum=10, maximum=86400),
        Field("verapdf_timeout_s", "int", 300, minimum=10, maximum=86400),
        Field("av_timeout_min_s", "int", 600, minimum=10, maximum=7 * 86400),
    )
}

SECTIONS: dict[str, dict[str, Field]] = {
    "workflow": WORKFLOW_FIELDS,
    "app": APP_FIELDS,
    "advanced": ADVANCED_FIELDS,
}

ENV_OVERRIDES = (
    "BALEEN_PORT", "BALEEN_SOFFICE", "BALEEN_FFMPEG", "BALEEN_FFPROBE",
    "BALEEN_JAVA_HOME", "BALEEN_VERAPDF",
)


def defaults() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        **{sec: {k: f.default for k, f in fields.items()} for sec, fields in SECTIONS.items()},
    }


def find_field(key: str) -> tuple[str, Field]:
    for sec, fields in SECTIONS.items():
        if key in fields:
            return sec, fields[key]
    raise KeyError(key)


def validate(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a settings document. Returns (clean settings, warnings)."""
    warnings: list[str] = []
    out = defaults()
    if not isinstance(raw, dict):
        return out, ["Settings file is not a JSON object; using defaults."]
    ver = raw.get("schema_version")
    if ver != SCHEMA_VERSION:
        warnings.append(f"Settings schema_version {ver!r} is not {SCHEMA_VERSION}; unknown keys ignored.")
    for sec, fields in SECTIONS.items():
        part = raw.get(sec, {})
        if not isinstance(part, dict):
            warnings.append(f"Settings section '{sec}' is invalid; using defaults for it.")
            continue
        for k, f in fields.items():
            if k not in part:
                continue
            try:
                out[sec][k] = f.coerce(part[k])
            except ValueError as e:
                warnings.append(f"Invalid setting {e}; using the default.")
    return out, warnings


def flat(settings: dict[str, Any]) -> dict[str, Any]:
    """All §11 keys in one mapping (workflow + app + advanced), for the run JSON."""
    d: dict[str, Any] = {}
    for sec in SECTIONS:
        d.update(settings.get(sec, {}))
    return d


def workflow_options(settings: dict[str, Any]) -> dict[str, Any]:
    """The workflow settings without the folder keys (what changes the plan or conversion)."""
    return {k: v for k, v in settings["workflow"].items() if k not in FOLDER_KEYS}


def apply_overrides(settings: dict[str, Any], pairs: list[str]) -> dict[str, Any]:
    """Apply CLI `--set key=value` pairs. Raises ValueError on unknown keys or bad values."""
    s = copy.deepcopy(settings)
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got {pair!r}")
        key, val = pair.split("=", 1)
        key = key.strip()
        try:
            sec, f = find_field(key)
        except KeyError as e:
            raise ValueError(f"Unknown setting {key!r}") from e
        s[sec][key] = f.coerce(val)
    return s


@dataclass
class LoadResult:
    settings: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def load(path: Path) -> LoadResult:
    if not path.exists():
        return LoadResult(defaults())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return LoadResult(defaults(), [f"Couldn't read {path.name} ({e.__class__.__name__}); using defaults."])
    clean, warnings = validate(raw)
    return LoadResult(clean, warnings)


def save(path: Path, settings: dict[str, Any]) -> None:
    """Atomic write: temp file in the same folder, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=False)
    tmp.write_text(data + "\n", encoding="utf-8")
    os.replace(tmp, path)


class SettingsStore:
    """Thread-safe settings holder. Changes save immediately and notify listeners (live apply)."""

    def __init__(self, path: Path | None, initial: dict[str, Any] | None = None,
                 warnings: list[str] | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        if initial is None:
            res = load(path) if path else LoadResult(defaults())
            initial, warnings = res.settings, res.warnings
        self._settings = initial
        self.warnings: list[str] = list(warnings or [])

    @classmethod
    def open(cls, path: Path) -> SettingsStore:
        return cls(path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._settings)

    def app(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings["app"])

    def workflow(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings["workflow"])

    def advanced(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings["advanced"])

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return unsubscribe

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Set several keys (any section, looked up by key). Raises ValueError on invalid values."""
        with self._lock:
            new = copy.deepcopy(self._settings)
            for key, raw in changes.items():
                sec, f = find_field(key)
                new[sec][key] = f.coerce(raw)
            self._settings = new
            if self._path:
                save(self._path, new)
            listeners = list(self._listeners)
            snap = copy.deepcopy(new)
        for fn in listeners:
            fn(snap)
        return snap

    def reset_workflow(self) -> dict[str, Any]:
        """'Reset to defaults' restores every workflow option except the folders (§11)."""
        d = defaults()["workflow"]
        return self.update({k: v for k, v in d.items() if k not in FOLDER_KEYS})

    def reset_resources(self) -> dict[str, Any]:
        """UI-S2 'Defaults': Balanced, Auto, Auto, on, on, data/work."""
        d = defaults()["app"]
        return self.update({k: d[k] for k in RESOURCE_KEYS})
