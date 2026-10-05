"""Plan (§5.3, §7): route and output name for every item, before anything is written.

The plan depends only on the source listing, `audio_container`, file contents seen by the
route probes, and e-mail contents (attachments). Shuffling scan order never changes a name
(§7.4, P7). Preview (UI-C4) displays exactly this plan.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import osutil
from .convert import formats
from .convert.base import ChildSpec, ProbeContext, SourceRef, get_route
from .model import REASONS, Action, Category, Mode, Plan, PlanItem, Probe, ScanEntry, Status
from .paths import any_component_too_long, fold_key, join_rel

MAX_EMAIL_DEPTH = 3  # §6.5: nested message/rfc822 processed to depth 3

TARGET_NAMES = {".jpg": "JPEG", ".tif": "TIFF", ".pdf": "PDF/A", ".mp4": "MP4", ".m4a": "M4A"}


def split_name(name: str) -> tuple[str, str]:
    """('report', '.doc') - the extension is the last suffix; a leading dot alone is not one."""
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    return name[:i], name[i:]


@dataclass
class _Node:
    item: PlanItem
    probe: Probe | None = None
    src: SourceRef | None = None
    children: list[_Node] = field(default_factory=list)


class Planner:
    def __init__(self, ctx: ProbeContext, source_root: str, output_root: str | None, *,
                 workers: int = 4, on_progress: Callable[[int, int], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.ctx = ctx
        self.source_root = source_root
        self.output_root = output_root
        self.workers = max(1, workers)
        self.on_progress = on_progress
        self.should_stop = should_stop
        self.mode = ctx.mode
        self.missing_tools = set(ctx.tools.missing())

    # ------------------------------------------------------------------ entry point

    def build(self, entries: list[ScanEntry]) -> Plan:
        nodes = [self._node_for_entry(e) for e in entries]
        to_probe = [nd for nd in nodes if nd.src is not None]
        self._probe_all(to_probe)
        if self.mode == Mode.CONVERT:
            self._expand_all(nodes, depth=1)
        flat: list[_Node] = []

        def visit(nd: _Node) -> None:
            flat.append(nd)
            for ch in nd.children:
                visit(ch)

        for nd in nodes:
            visit(nd)
        for i, nd in enumerate(flat, start=1):
            nd.item.n = i
        for nd in flat:
            for ch in nd.children:
                ch.item.parent = nd.item.n
        if self.mode == Mode.CONVERT:
            self._assign_outputs(flat)
        for nd in flat:
            self._finalise(nd)
        return Plan(
            items=[nd.item for nd in flat],
            source_root=self.source_root,
            output_root=self.output_root,
            mode=self.mode,
            settings=self.ctx.settings,
        )

    # ------------------------------------------------------------------ nodes

    def _node_for_entry(self, e: ScanEntry) -> _Node:
        name = e.name
        _stem, ext = split_name(name)
        ext_l = ext.lower()
        item = PlanItem(
            n=0, source_path=e.rel, abs_path=join_rel(self.source_root, e.rel), size=e.size,
            mtime_ns=e.mtime_ns, ext=ext_l, category=Category.OTHER, action=Action.NONE, route=None,
            target_ext=None, out_dir=e.rel_dir, output_path=None, is_dir=e.is_dir,
        )
        if e.ignore:
            item.reasons = [e.ignore]
            item.final = True
            return _Node(item)
        fmt = formats.lookup(ext_l)
        if fmt is None:
            item.reasons = ["UNSUPPORTED_FORMAT"]
            item.final = True
            item.source_format = ext_l.lstrip(".").upper() if ext_l else ""
            return _Node(item)
        item.route = fmt.route
        item.category = fmt.category
        item.target_ext = formats.default_target(ext_l, self.ctx.settings)
        src = SourceRef(name=name, ext=ext_l, path=item.abs_path, size=e.size)
        return _Node(item, src=src)

    def _probe_one(self, nd: _Node) -> None:
        assert nd.src is not None and nd.item.route is not None
        route = get_route(nd.item.route)
        try:
            pr = route.probe(self.ctx, nd.src)
        except Exception as e:  # a probe never stops the plan (P6)
            pr = Probe(category=nd.item.category, target_ext=nd.item.target_ext, action=Action.CONVERT,
                       message=f"Probe failed: {e.__class__.__name__}: {e}", route=nd.item.route)
        nd.probe = pr

    def _probe_all(self, nodes: list[_Node]) -> None:
        if not nodes:
            return
        done = 0
        total = len(nodes)

        def work(nd: _Node) -> None:
            osutil.set_thread_low_priority(self.ctx.low_priority)
            if self.should_stop and self.should_stop():
                return
            self._probe_one(nd)

        with ThreadPoolExecutor(max_workers=min(self.workers, total), thread_name_prefix="baleen-probe") as ex:
            for _ in ex.map(work, nodes):
                done += 1
                if self.on_progress and (done % 50 == 0 or done == total):
                    self.on_progress(done, total)
        for nd in nodes:
            self._apply_probe(nd)

    def _apply_probe(self, nd: _Node) -> None:
        pr, it = nd.probe, nd.item
        if pr is None:
            return
        it.category = pr.category
        it.target_ext = pr.target_ext
        it.action = pr.action
        it.source_format = pr.source_format
        it.method = pr.method
        it.reasons = list(pr.reasons)
        it.notes = list(pr.notes)
        it.message = pr.message
        # Merge, never replace: attachments carry planner data (sha256, child hints) that the
        # report needs even when the probe decides the item at plan time.
        it.data = {**it.data, **pr.data}
        if pr.route:
            it.route = pr.route
        # Only reasons that mean "nothing will be written" decide an item at plan time; a
        # reason like CHARSET_ERRORS (written: yes) is carried into the run instead.
        it.final = pr.final or any(decides_at_plan_time(c) for c in pr.reasons)

        if self.mode == Mode.CHECK:
            if not it.final and it.action in (Action.CONVERT, Action.REMUX):
                target = TARGET_NAMES.get(it.target_ext or "", (it.target_ext or "").lstrip(".").upper())
                it.reasons = ["NOT_ARCHIVAL_FORMAT"]
                it.message = f"Convert would turn it into {target} ({it.target_ext})."
                it.final = True
            elif it.action == Action.COPY:
                it.action = Action.CHECK
            return

        # Convert mode
        if it.action == Action.COPY and not self.ctx.workflow.get("copy_existing", True):
            it.action = Action.CHECK  # DR-06: checked in place only
        if not it.final and it.action in (Action.CONVERT, Action.REMUX) and it.route:
            route = get_route(it.route)
            missing = [t for t in route.converter_tools if t in self.missing_tools]
            if missing:
                it.reasons = ["TOOL_MISSING"]
                it.message = "Missing: " + ", ".join(missing)
                it.final = True

    # ------------------------------------------------------------------ e-mail attachments

    def _expand_all(self, nodes: list[_Node], depth: int) -> None:
        for nd in nodes:
            it = nd.item
            # Every parseable e-mail is expanded, even when it is decided at plan time
            # (TOOL_MISSING, EML_ATTACHMENTS_BLOCKED): names stay stable across settings (§7.3),
            # and under `extract` the attachments are still processed as their own items.
            if it.route != "email" or nd.src is None:
                continue
            if set(it.reasons) & {"CONVERSION_ERROR", "EML_NESTING_TOO_DEEP", "SOURCE_UNREADABLE"}:
                continue
            route = get_route("email")
            try:
                specs = route.expand(self.ctx, it, nd.src)
            except Exception as e:
                if not it.final:
                    it.reasons = ["CONVERSION_ERROR"]
                    it.message = f"Couldn't read the e-mail: {e.__class__.__name__}: {e}"
                    it.final = True
                continue
            policy = self.ctx.workflow.get("eml_attachments", "extract")
            for spec in specs:
                ch = self._child_node(nd, spec, depth, materialise=(policy == "extract") and it.materialise)
                nd.children.append(ch)
            if nd.children:
                self._probe_all([c for c in nd.children if c.src is not None])
                nested = [c for c in nd.children if c.item.route == "email" and not c.item.final]
                if nested:
                    if depth + 1 > MAX_EMAIL_DEPTH:
                        for c in nested:
                            c.item.reasons = ["EML_NESTING_TOO_DEEP"]
                            c.item.message = (f"Forwarded e-mails are nested more than {MAX_EMAIL_DEPTH} deep; "
                                              "this message was not converted.")
                            c.item.final = True
                    else:
                        self._expand_all(nested, depth + 1)

    def _child_node(self, parent: _Node, spec: ChildSpec, depth: int, materialise: bool) -> _Node:
        ext_l = split_name(spec.name)[1].lower()
        p = parent.item
        it = PlanItem(
            n=0, source_path=f"{p.source_path}#{spec.name}", abs_path=None, size=len(spec.data),
            mtime_ns=None, ext=ext_l, category=Category.OTHER, action=Action.NONE, route=None,
            target_ext=None, out_dir="", output_path=None, depth=depth, materialise=materialise,
            message=spec.message,
        )
        it.data["sha256"] = hashlib.sha256(spec.data).hexdigest()
        it.data["child"] = dict(spec.data_hint)
        fmt = formats.lookup(ext_l)
        if fmt is None:
            it.reasons = ["UNSUPPORTED_FORMAT"]
            it.final = True
            it.source_format = ext_l.lstrip(".").upper() if ext_l else ""
            return _Node(it)
        it.route, it.category = fmt.route, fmt.category
        it.target_ext = formats.default_target(ext_l, self.ctx.settings)
        return _Node(it, src=SourceRef(name=spec.name, ext=ext_l, data=spec.data, size=len(spec.data)))

    # ------------------------------------------------------------------ output names (§7)

    def _assign_outputs(self, flat: list[_Node]) -> None:
        # Attachments' folders depend on the parent's final name, so resolve directories in
        # order of depth: an attachment folder is always deeper than its e-mail's folder.
        pending = [nd for nd in flat if nd.item.parent is None]
        while pending:
            groups: dict[str, list[_Node]] = {}
            for nd in pending:
                groups.setdefault(fold_key(nd.item.out_dir), []).append(nd)
            for _key, members in sorted(groups.items(), key=lambda kv: kv[0].count("/")):
                self._resolve_dir(members)
            nxt: list[_Node] = []
            for nd in pending:
                if nd.children:
                    base_stem = split_name(nd.item.output_path.rsplit("/", 1)[-1])[0] if nd.item.output_path else (
                        split_name(nd.item.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1])[0])
                    folder = f"{base_stem}_attachments"
                    out_dir = f"{nd.item.out_dir}/{folder}" if nd.item.out_dir else folder
                    for ch in nd.children:
                        ch.item.out_dir = out_dir
                        nxt.append(ch)
            pending = nxt

    def _resolve_dir(self, members: list[_Node]) -> None:
        """Clash rule (§7.3, DR-05) for every item planned into one output directory."""
        parts = [nd for nd in members if nd.item.route and nd.item.target_ext and not nd.item.is_dir]
        if not parts:
            return

        def src_name(it: PlanItem) -> str:
            return it.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1]

        names: dict[int, str] = {}
        for nd in parts:
            stem, _ = split_name(src_name(nd.item))
            names[nd.item.n] = stem + (nd.item.target_ext or "")
        keys: dict[str, list[int]] = {}
        for n, nm in names.items():
            keys.setdefault(fold_key(nm), []).append(n)
        clashed = {n for ns in keys.values() if len(ns) > 1 for n in ns}
        for nd in parts:
            if nd.item.n in clashed:
                stem, ext = split_name(src_name(nd.item))
                names[nd.item.n] = f"{stem}_{ext.lower().lstrip('.')}{nd.item.target_ext}"
                nd.item.clash_renamed = True
        keys2: dict[str, list[int]] = {}
        for n, nm in names.items():
            keys2.setdefault(fold_key(nm), []).append(n)
        unresolved = {n for ns in keys2.values() if len(ns) > 1 for n in ns}
        for nd in parts:
            it = nd.item
            it.output_path = f"{it.out_dir}/{names[it.n]}" if it.out_dir else names[it.n]
            if it.n in unresolved:
                if "NAME_CLASH_UNRESOLVED" not in it.reasons:
                    it.reasons = ["NAME_CLASH_UNRESOLVED", *it.reasons]
                it.message = "Another file in this folder would get the same output name."
                it.final = True

    # ------------------------------------------------------------------ last pass

    def _finalise(self, nd: _Node) -> None:
        it = nd.item
        if it.is_dir or "SYSTEM_FILE" in it.reasons or "SYMLINK" in it.reasons:
            it.final = True
            it.action = Action.NONE
            return
        if self.mode == Mode.CONVERT and it.output_path and not it.final:
            if any_component_too_long(it.output_path) or any_component_too_long(
                    it.source_path.replace("#", "/")):
                it.reasons = ["PATH_TOO_LONG"]
                it.message = "A folder or file name is longer than 255 characters."
                it.final = True
        if it.final and it.reasons and it.action in (Action.CONVERT, Action.REMUX, Action.COPY):
            # Nothing will be converted or copied for items decided at plan time.
            it.action = Action.CHECK if self.mode == Mode.CHECK else it.action
        if it.reasons and "UNSUPPORTED_FORMAT" in it.reasons:
            it.action = Action.NONE


def decides_at_plan_time(code: str) -> bool:
    r = REASONS[code]
    return r.status in (Status.UNSUPPORTED, Status.IGNORED, Status.SKIPPED) or r.written == "no"


def output_abs(output_root: str, item: PlanItem) -> str | None:
    if not item.output_path:
        return None
    return join_rel(output_root, item.output_path)


def plan_summary(plan: Plan) -> dict[str, Any]:
    """Preview payload (UI-C4): counts, category bars, clash renames, plan-time problems."""
    counts = plan.counts()
    renames = [
        {"source": it.source_path, "output": it.output_path}
        for it in plan.items
        if it.clash_renamed and it.materialise
    ]
    problems = [
        {"source": it.source_path, "reasons": list(it.reasons), "message": it.message}
        for it in plan.items
        if it.materialise and it.reasons
        and not set(it.reasons) & {"SYSTEM_FILE", "SYMLINK", "UNSUPPORTED_FORMAT"}
    ]
    return {"counts": counts, "renames": renames, "problems": problems}
