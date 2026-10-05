"""Workflow registry (spec §12.8, design §02, D-02, D-05).

A workflow is a module that registers one Workflow object. The rail item, setup page,
options panel, confirmation and run page are generated from it; adding a workflow MUST NOT
require changes to shared templates.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..model import Mode


@dataclass(frozen=True)
class FolderField:
    key: str  # settings key holding the last-used value (source_dir, output_dir, check_dir)
    label: str
    role: str  # "read" | "write"
    help: str
    placeholder: str = ""


@dataclass(frozen=True)
class OptionField:
    key: str
    label: str
    help: str
    type: str  # "seg" | "switch" | "select"
    values: tuple[tuple[str, str], ...] = ()
    default: Any = None


@dataclass(frozen=True)
class OptionGroup:
    group: str
    fields: tuple[OptionField, ...]


@dataclass(frozen=True)
class Explainer:
    title: str
    lines: tuple[str, ...]  # "Lead · rest": the UI shows the lead in bold
    footer: str = ""
    icons: tuple[str, ...] = ()  # sprite ids (i-<icon>), one per line


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    icon: str  # sprite id suffix: i-<icon>
    order: int
    description: str
    kicker: str
    mode: Mode
    folders: tuple[FolderField, ...]
    options: tuple[OptionGroup, ...] = ()
    preview: bool = False
    start_label: str = "Start"
    confirm: bool = False
    result_columns: tuple[str, ...] = ("status", "source", "output", "action", "reason")
    explainer: Explainer | None = None
    ready_text: str = "Ready."
    reassurance: str = ""
    # plan(engine, settings) -> Plan and run(engine, settings) -> Job are generic for the
    # built-in workflows; custom workflows may override them.
    plan: Callable[..., Any] | None = None
    run: Callable[..., Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def route(self) -> str:
        return f"/{self.id}"

    def option_keys(self) -> list[str]:
        return [f.key for g in self.options for f in g.fields]

    def folder_keys(self) -> list[str]:
        return [f.key for f in self.folders]


_REGISTRY: dict[str, Workflow] = {}


def register(wf: Workflow) -> Workflow:
    if wf.id in _REGISTRY:
        raise ValueError(f"workflow {wf.id!r} registered twice")
    _REGISTRY[wf.id] = wf
    return wf


def _load() -> None:
    if _REGISTRY:
        return
    for mod in ("convert", "check"):
        importlib.import_module(f"baleen.workflows.{mod}")


def all_workflows() -> list[Workflow]:
    _load()
    return sorted(_REGISTRY.values(), key=lambda w: w.order)


def get(wid: str) -> Workflow | None:
    _load()
    return _REGISTRY.get(wid)
