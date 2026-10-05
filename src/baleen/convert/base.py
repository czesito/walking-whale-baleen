"""The converter contract: routes, sources, work items (spec §5.4, §6, §8).

A *route* handles one family of formats (image, document, text, html, email, pdf, media).
The runner moves every item through the same steps; a route only fills in what is
format-specific:

    plan time   probe(ctx, src)        -> Probe     refine target/action, plan-time problems
                expand(ctx, item, src) -> children  e-mail attachments (planned for naming)
    run time    process(ctx, work)                  convert or check, then local checks (§8)
                prepare/finish(...)                 Documents-lane routes: around one soffice batch
                stage_children(ctx, work)           e-mail: write attachments to the work folder

Rules every route MUST follow:
- P1: never write next to a source. Converters only touch `work.staged` (a copy in the work
  folder) and files inside `work.work_dir`. Check-only items read `work.source_abs` in place.
- P4/DR-09: put every check result in `work.checks`. The runner applies the §8 outcome rules;
  a route never publishes and never decides "placed" itself.
- DR-08: never treat a PDF/A claim as validation. Request V-PDFA by setting `work.pdfa`.
- Use `ctx.tokens` for tool thread counts (FFmpeg `-threads`), and `tools_env` / proc.run
  with `low_priority=ctx.low_priority` for every child process (SEC-8, §5.5).
"""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar

from ..home import Home
from ..model import Action, Category, CheckResult, Mode, PlanItem, Probe, outcome_from_checks
from ..paths import long_path
from ..scheduler import Lane, TaskContext
from ..tools import Toolset


@dataclass
class SourceRef:
    """A source to probe: a file on disk (read-only) or bytes held in memory (attachments)."""

    name: str  # file name (for attachments: the sanitised name)
    ext: str  # lower-case extension with dot
    path: str | None = None  # absolute path on disk
    data: bytes | None = None
    size: int | None = None

    def open(self) -> IO[bytes]:
        if self.data is not None:
            return io.BytesIO(self.data)
        assert self.path is not None
        return open(long_path(self.path), "rb")  # noqa: SIM115

    def read_head(self, n: int = 65536) -> bytes:
        with self.open() as f:
            return f.read(n)


@dataclass
class RunContext:
    """Run-wide facts every route may read. Immutable during a run except `settings` (live app prefs)."""

    run_id: str
    mode: Mode
    home: Home
    tools: Toolset
    settings: dict[str, Any]  # full settings snapshot (workflow / app / advanced) at run start
    source_root: str
    output_root: str | None
    work_root: str  # <work>/<run-id>
    app_prefs: Callable[[], dict[str, Any]] = field(default=lambda: {})

    @property
    def workflow(self) -> dict[str, Any]:
        return self.settings["workflow"]

    @property
    def advanced(self) -> dict[str, Any]:
        return self.settings["advanced"]

    def tools_env(self) -> dict[str, str]:
        return self.tools.env()


@dataclass
class ProbeContext:
    """What probe() and expand() may use. Probing never writes anything."""

    mode: Mode
    settings: dict[str, Any]
    tools: Toolset
    home: Home
    low_priority: bool = True

    @property
    def workflow(self) -> dict[str, Any]:
        return self.settings["workflow"]


@dataclass
class ChildSpec:
    """An e-mail attachment (or nested e-mail) found by expand(). Becomes a PlanItem."""

    name: str  # sanitised file name (§7.8) - the output stem comes from it
    data: bytes  # content, so the child can be probed without writing anything
    depth: int  # nesting depth of the e-mail that contains it (top-level e-mail = 1)
    message: str = ""
    data_hint: dict[str, Any] = field(default_factory=dict)  # route-private (e.g. MIME type, cid)


@dataclass
class LoJob:
    """One input for a LibreOffice call (§6.2). Built by prepare(); consumed by the Documents lane."""

    input_path: str  # staging input inside the work folder, unique name per batch
    out_dir: str  # where soffice writes <stem>.pdf
    export_filter: str  # writer_pdf_Export | calc_pdf_Export | impress_pdf_Export
    pdfa_level: str  # "1b" | "2b" | "3b"
    infilter: str | None = None  # "Text (encoded):..." | "HTML (StarWriter)" | None

    @property
    def batch_key(self) -> str:
        return f"{self.infilter or ''}|{self.export_filter}|{self.pdfa_level}"

    @property
    def expected_output(self) -> str:
        return os.path.join(self.out_dir, Path(self.input_path).stem + ".pdf")


@dataclass
class LoResult:
    job: LoJob
    ok: bool
    output: str | None  # path of the produced PDF
    reason: str | None = None  # CONVERSION_ERROR | TIMEOUT | PASSWORD_PROTECTED | TOOL_MISSING
    message: str = ""
    method: str = ""  # e.g. "LibreOffice 26.8.0 · writer_pdf_Export · PDF/A-2b"
    retried_alone: bool = False


@dataclass
class WorkItem:
    """Run-time state of one item while it moves through the lanes."""

    plan: PlanItem
    run: RunContext
    work_dir: str  # <work>/<run-id>/<n>
    source_abs: str | None  # absolute source path (None for attachments)
    check_only: bool  # verify in place, read-only; nothing staged or published
    staged: str | None = None  # local copy of the source in the work folder
    source_sha256: str = ""
    source_size: int | None = None
    source_format: str = ""
    category: Category = Category.OTHER
    action: Action = Action.NONE
    method: str = ""
    result_path: str | None = None  # local, verified output ready to publish
    new_output: bool = False  # True: result is a new file (convert/remux) -> fails are VERIFY_FAILED
    pdfa: tuple[str, str] | None = None  # (path to validate, flavour "1b|2b|3b|<claimed>") -> PDF/A lane
    resume: bool = False  # final path existed and a prior report matched: re-check only
    existing_output: str | None = None  # absolute path of the existing output (resume)
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    done: bool = False  # outcome decided; skip to report
    extra: dict[str, Any] = field(default_factory=dict)  # route-private scratch space
    children: list[WorkItem] = field(default_factory=list)  # e-mail attachments staged by this item

    @property
    def n(self) -> int:
        return self.plan.n

    @property
    def settings(self) -> dict[str, Any]:
        return self.run.workflow

    def input_path(self) -> str:
        """The file a route must read: the staged copy, or the source in place for check-only items."""
        if self.resume and self.existing_output:
            return self.existing_output
        if self.staged:
            return self.staged
        assert self.source_abs is not None
        return self.source_abs

    def fail(self, reason: str, message: str = "") -> None:
        self.reasons.append(reason)
        if message:
            self.messages.append(message)
        self.done = True

    def note(self, code: str) -> None:
        if code not in self.notes:
            self.notes.append(code)

    def out(self, name: str) -> str:
        """A path inside this item's work folder."""
        os.makedirs(long_path(self.work_dir), exist_ok=True)
        return os.path.join(self.work_dir, name)


class Route:
    """Base class for routes. Subclasses override what they need.

    Attributes:
        key: route id referenced by PlanItem.route.
        lane: lane for the process step (Files, Documents or Media).
        converter_tools: tools needed to convert (missing -> TOOL_MISSING at plan time).
        batched: True for Documents-lane routes that implement prepare()/finish().
    """

    key: ClassVar[str] = ""
    lane: ClassVar[Lane] = Lane.FILES
    converter_tools: ClassVar[tuple[str, ...]] = ()
    batched: ClassVar[bool] = False
    infilter: ClassVar[str | None] = None  # LibreOffice import filter for batched routes

    def batch_key(self, work: WorkItem) -> str:
        """Documents lane: items with the same key may share one soffice call (DR-35)."""
        from .formats import EXPORT_FILTER

        export = EXPORT_FILTER.get(work.plan.ext, "writer_pdf_Export")
        return f"{self.infilter or ''}|{export}|{work.settings.get('pdfa_level', '2b')}"

    def decide(self, work: WorkItem) -> list[str]:
        """Reason codes implied by work.checks (§8 outcome rules). Routes may refine the mapping,
        e.g. an existing PDF failing V-PDFA is PDFA_INVALID (§6.6)."""
        return outcome_from_checks(work.checks, new_output=work.new_output)

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        raise NotImplementedError

    def expand(self, ctx: ProbeContext, item: PlanItem, src: SourceRef) -> list[ChildSpec]:
        """Attachments of an e-mail (planned even under block/list, for stable names)."""
        return []

    def memory_mb(self, work: WorkItem) -> int | None:
        """Files-lane memory reservation (images over 32 MP); None = lane default."""
        return None

    def stage_children(self, ctx: TaskContext, work: WorkItem) -> None:
        """E-mail: write each child's bytes into child.work_dir and set child.staged."""
        return None

    # Unbatched routes (Files and Media lanes)
    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        raise NotImplementedError

    # Batched routes (Documents lane, DR-35)
    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        """Write the staging input for LibreOffice; return None if the item is already decided."""
        raise NotImplementedError

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        """After soffice: set result_path, run V-PDF-OPEN (+V-TEXT), request V-PDFA via work.pdfa."""
        raise NotImplementedError

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        """§7.5 resume: re-run the checks on work.existing_output (read-only). Default: process()."""
        self.process(ctx, work)


_registry: dict[str, Route] = {}
_lock = threading.Lock()


def register(route: Route) -> Route:
    """Called by each route module at import time."""
    with _lock:
        _registry[route.key] = route
    return route


def get_route(key: str) -> Route:
    _load_builtin()
    r = _registry.get(key)
    if r is None:
        from .formats import MissingRoute

        return MissingRoute(key)
    return r


def all_routes() -> dict[str, Route]:
    _load_builtin()
    return dict(_registry)


_loaded = False
_load_lock = threading.Lock()  # separate from _lock: register() takes _lock during the imports


def _load_builtin() -> None:
    """Import the built-in route modules once; each registers itself.

    `_loaded` is set only after every import finished, so a concurrent caller (parallel plan-time
    probes) waits for the registry instead of finding it half-filled.
    """
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        import importlib

        for mod in ("image", "document", "text", "html", "email", "pdf", "media"):
            try:
                importlib.import_module(f"baleen.convert.{mod}")
            except ModuleNotFoundError as e:
                if e.name != f"baleen.convert.{mod}":
                    raise
        _loaded = True
