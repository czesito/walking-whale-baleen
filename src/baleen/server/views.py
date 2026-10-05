"""View models for the templates: folder validation, action bar, preview card, tool notices,
resource use, run pages, the inspector and the history table. Pure functions of the server
context, so they are easy to test and the route handlers stay thin."""

from __future__ import annotations

import csv
import io
import os
from typing import Any

from markupsafe import Markup

from ..model import CSV_COLUMNS, Mode, Status, parse_checks
from ..paths import free_bytes, is_network_path, long_path, overlap
from ..report import RUN_INDEX_LIMIT
from ..runner import FINAL_STATES, Job
from ..scheduler import Machine, resolve_budget
from ..settings import APP_FIELDS, RESOURCE_KEYS, defaults
from ..tools import SPECS, TOOL_KEYS
from ..workflows import Workflow, all_workflows
from ..workflows import get as get_workflow
from .context import ServerContext
from .present import (
    CHECK_STATE,
    KEY_TO_STATUS,
    STATUS_KEYS,
    codes,
    duration,
    elapsed,
    gb_free,
    is_note,
    num,
    reason_help,
    reason_label,
    span,
    split_path,
    status_key,
)
from .runviews import RunView, current_job, journal_of

PROC_HELP = {
    "gentle": "Slowest. The computer stays fully responsive. Good during the working day.",
    "balanced": "Recommended for most runs.",
    "maximum": "Fastest. The computer may feel slow. Good for overnight runs.",
    "custom": "Choose exactly how many cores Baleen may use.",
}
IN_PROGRESS_SHOWN = 6
PAGE_SIZE = 100


def clean_path(raw: Any) -> str:
    """A pasted path: trimmed, surrounding quotes removed (Explorer's "Copy as path")."""
    s = ("" if raw is None else str(raw)).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


# --------------------------------------------------------------------------- folders (UI-C1)


def _readable(p: str) -> bool:
    try:
        with os.scandir(long_path(p)) as it:
            next(it, None)
        return True
    except OSError:
        return False


def _nearest_existing(p: str) -> str:
    q = os.path.abspath(p)
    while not os.path.exists(long_path(q)):
        parent = os.path.dirname(q)
        if parent == q:
            break
        q = parent
    return q


def validate_fields(wf: Workflow, values: dict[str, str]) -> dict[str, dict[str, str] | None]:
    """Status line per folder field: {'ok': text} | {'error': text} | None (empty field).

    Exists, readable / writable, and the overlap rule of §5.1 (real paths, case-insensitive on
    macOS and Windows). Validation never writes anything.
    """
    has_write = any(f.role == "write" for f in wf.folders)
    out: dict[str, dict[str, str] | None] = {}
    read_paths = [values.get(f.key, "") for f in wf.folders if f.role == "read"]
    for f in wf.folders:
        v = values.get(f.key, "")
        if not v:
            out[f.key] = None
            continue
        if not os.path.isabs(v):
            out[f.key] = {"error": "Use the folder's full path, starting with the drive or volume."}
            continue
        if f.role == "read":
            if not os.path.exists(long_path(v)):
                out[f.key] = {"error": f"Folder not found: {v}"}
            elif not os.path.isdir(long_path(v)):
                out[f.key] = {"error": f"{v} is a file, not a folder."}
            elif not _readable(v):
                out[f.key] = {"error": f"Baleen can't read {v}."}
            else:
                out[f.key] = {"ok": "Readable · read-only for Baleen" if has_write else "Readable"}
            continue
        # write role
        err = ""
        for rp in read_paths:
            if not rp or not os.path.isabs(rp):
                continue
            rel = overlap(rp, v)
            if rel == "same":
                err = "The output folder is the source folder. Choose a different folder."
            elif rel == "b_in_a":
                err = "This folder is inside the source folder. Choose a folder outside it."
            elif rel == "a_in_b":
                err = "The source folder is inside this folder. Choose a folder outside it."
            if err:
                break
        if not err and os.path.exists(long_path(v)) and not os.path.isdir(long_path(v)):
            err = f"{v} is a file, not a folder."
        if not err:
            base = v if os.path.isdir(long_path(v)) else _nearest_existing(v)
            if not os.path.isdir(long_path(base)) or not os.access(long_path(base), os.W_OK):
                err = f"Baleen can't write to {v}."
        if err:
            out[f.key] = {"error": err}
        else:
            base = v if os.path.isdir(long_path(v)) else _nearest_existing(v)
            out[f.key] = {"ok": f"Writable · {gb_free(free_bytes(base))} free"}
    return out


def ready(statuses: dict[str, dict[str, str] | None]) -> bool:
    return all(s is not None and "ok" in s for s in statuses.values())


def roots_for(wf: Workflow, values: dict[str, str]) -> tuple[str, str | None]:
    source = next((values.get(f.key, "") for f in wf.folders if f.role == "read"), "")
    output = next((values.get(f.key, "") for f in wf.folders if f.role == "write"), None)
    return source, output


# --------------------------------------------------------------------------- preview & action bar


def preview_view(ctx: ServerContext, wf: Workflow, statuses: dict[str, Any], *,
                 announce: bool = False) -> dict[str, Any]:
    st = ctx.previews.get(wf.id)
    is_ready = ready(statuses)
    view: dict[str, Any] = {"state": "none", "ready": is_ready, "announce": ""}
    if st is None:
        return view
    key = ctx.preview_key(wf)
    if st.state == "running":
        view.update(state="running", source=st.source, scanned=st.scanned)
        return view
    if st.state == "error":
        if st.key == key:
            view.update(state="error", error=st.error)
        return view
    s = st.summary or {}
    c = s.get("counts", {})
    parts = []
    if s.get("problems_total"):
        parts.append(f"{num(s['problems_total'])} to look at")
    if s.get("unsupported_total"):
        parts.append(f"{num(s['unsupported_total'])} unsupported")
    view.update(state="done" if st.key == key else "stale", summary=s, known_chip=" · ".join(parts))
    if announce and view["state"] == "done":
        view["announce"] = f"Preview ready: {num(c.get('files', 0))} files, {num(c.get('convert', 0))} to convert"
    return view


def actionbar_view(ctx: ServerContext, wf: Workflow, statuses: dict[str, Any],
                   prv: dict[str, Any] | None) -> dict[str, Any]:
    running = current_job(ctx) is not None
    is_ready = ready(statuses)
    if running:
        return {"kind": "busy", "text": wf.extra.get("busy_text", "Wait for the current run to finish."),
                "can_start": False}
    if not is_ready:
        return {"kind": "info", "text": wf.extra.get("idle_text", "Choose the folders above."), "can_start": False}
    if wf.preview and prv and prv.get("state") == "done":
        c = prv["summary"]["counts"]
        return {"kind": "ok", "text": f"{num(c['files'])} files · {num(c['convert'])} to convert · preview up to date",
                "can_start": True}
    return {"kind": "info" if wf.preview else "ok", "text": wf.ready_text, "can_start": True}


def tool_notices(ctx: ServerContext, mode: Mode) -> list[dict[str, Any]]:
    """UI-C3: missing tools name the affected files and link to Tools. Starting stays allowed."""
    missing = set(ctx.tools.missing())
    out: list[dict[str, Any]] = []
    if mode == Mode.CONVERT and "libreoffice" in missing:
        out.append({"title": "LibreOffice isn't available", "body": SPECS["libreoffice"].affects})
    for k in ("ffmpeg", "ffprobe"):
        if k in missing:
            out.append({"title": f"{SPECS[k].name} isn't available", "body": SPECS[k].affects})
    if "verapdf" in missing or "java" in missing:
        name = "veraPDF" if "verapdf" in missing else SPECS["java"].name
        out.append({"title": f"{name} isn't available",
                    "body": Markup("PDF/A can't be verified. New PDFs will still be written but marked "
                                   "<b>Needs review</b>.")})
    return out


def chips_for(wf: Workflow | None, settings: dict[str, Any]) -> list[str]:
    """Settings chips (UI-C5, UI-R1) from the workflow's chip formats, else from its schema."""
    if wf is None or not wf.options:
        return []
    fmts = wf.extra.get("chips")
    out: list[str] = []
    if fmts:
        for key, fmt in fmts:
            if key not in settings:
                continue
            v = settings[key]
            if isinstance(v, bool) and "{onoff}" not in fmt:
                if v:
                    out.append(fmt)
                continue
            out.append(fmt.format(value=v, onoff="on" if v else "off"))
        return out
    for g in wf.options:
        for f in g.fields:
            if f.key in settings:
                v = settings[f.key]
                label = dict(f.values).get(v, v) if f.values else ("on" if v else "off")
                out.append(f"{f.label}: {label}")
    return out


# --------------------------------------------------------------------------- resource use (UI-S2)


def plan_view(app: dict[str, Any], machine: Machine) -> dict[str, Any]:
    b = resolve_budget(app, machine)
    return {"B": b.B, "K": b.K, "F": b.F, "threads": b.threads, "T": b.T, "M": b.M,
            "cores": machine.cores, "network": machine.network}


def all_folder_paths(ctx: ServerContext) -> list[str]:
    w = ctx.store.workflow()
    keys = {f.key for wf in all_workflows() for f in wf.folders}
    return [w[k] for k in sorted(keys) if w.get(k)]


def resource_view(ctx: ServerContext, *, work_error: str = "", work_value: str | None = None) -> dict[str, Any]:
    app = ctx.store.app()
    machine = ctx.machine(all_folder_paths(ctx))
    b = resolve_budget(app, machine)
    work = ctx.home.resolve_work_dir(app.get("work_dir", "data/work"))
    d = defaults()["app"]
    mem_max = max(1, machine.ram_gb - 2)
    return {
        "cores": machine.cores, "ram": machine.ram_gb, "network": machine.network,
        "work_free": gb_free(free_bytes(work)), "work_dir": work_value if work_value is not None else str(work),
        "work_network": is_network_path(str(work)), "work_error": work_error,
        "processor_use": app["processor_use"], "proc_help": PROC_HELP.get(app["processor_use"], ""),
        "processor_cores": min(machine.cores, max(1, int(app["processor_cores"]))),
        "memory_limit": app["memory_limit"], "memory_gb": min(mem_max, max(1, int(app["memory_gb"]))),
        "mem_max": mem_max, "auto_M": resolve_budget({**app, "memory_limit": "auto"}, machine).M,
        "transfer_slots": app["transfer_slots"], "transfer_count": int(app["transfer_count"]),
        "auto_T": 4 if machine.network else 8,
        "low_priority": bool(app["low_priority"]), "keep_awake": bool(app["keep_awake"]),
        "at_defaults": all(app.get(k) == d[k] for k in RESOURCE_KEYS),
        "B": b.B, "plan": plan_view(app, machine),
    }


def plan_overrides(app: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
    """GET /settings/plan may preview unsaved values (the memory slider while dragging)."""
    out = dict(app)
    for k, v in query.items():
        f = APP_FIELDS.get(k)
        if f is None or k not in RESOURCE_KEYS:
            continue
        try:
            out[k] = f.coerce(v)
        except ValueError:
            continue
    return out


def validate_work_dir(ctx: ServerContext, raw: str) -> tuple[str | None, str]:
    """Work folder (§11): a writable local folder outside source and output. Returns
    (value to store, error). The default location is stored as 'data/work'."""
    v = clean_path(raw)
    if not v or v == "data/work":
        return "data/work", ""
    if not os.path.isabs(v):
        return None, "Use the folder's full path, starting with the drive or volume."
    for p in all_folder_paths(ctx):
        rel = overlap(v, p)
        if rel:
            return None, "The work folder must be outside the source and output folders."
    if is_network_path(v):
        return None, "The work folder must be on a local disk."
    if os.path.exists(long_path(v)) and not os.path.isdir(long_path(v)):
        return None, f"{v} is a file, not a folder."
    base = v if os.path.isdir(long_path(v)) else _nearest_existing(v)
    if not os.path.isdir(long_path(base)) or not os.access(long_path(base), os.W_OK):
        return None, f"Baleen can't write to {v}."
    if os.path.normcase(os.path.abspath(v)) == os.path.normcase(str(ctx.home.default_work_dir)):
        return "data/work", ""
    return v, ""


# --------------------------------------------------------------------------- rail job card (UI-G2)


def finish_text(name: str, state: str, total: int, issues: int, processed: int) -> str:
    if state == "cancelled":
        return f"{name} cancelled · {num(processed)} of {num(total)} files"
    if state == "stopped":
        return f"{name} stopped · {num(processed)} of {num(total)} files"
    tail = f"{num(issues)} to look at" if issues else "all OK"
    return f"{name} finished · {num(total)} files · {tail}"


def railjob_view(job: Job | None, *, poll: bool = True) -> dict[str, Any]:
    empty = {"running": False, "poll": False, "state": "", "id": "", "pct": 0, "wf_name": "", "url": "",
             "label": "", "finish_text": ""}
    if job is None:
        return empty
    wf = get_workflow(job.workflow)
    name = wf.name if wf else job.workflow.capitalize()
    p = job.progress()
    running = job.state not in FINAL_STATES
    counts = p["counts"]
    issues = sum(int(counts.get(s.value, 0)) for s in (Status.FAILED, Status.NEEDS_REVIEW, Status.UNSUPPORTED))
    processed = sum(int(v) for v in counts.values()) - int(counts.get(Status.SKIPPED.value, 0))
    return {
        "running": running, "poll": running and poll, "state": job.state.value, "id": job.id,
        "pct": int(p["percent"]), "wf_name": name, "url": f"/runs/{job.id}",
        "label": (wf.extra.get("running_label") if wf else None) or name,
        "finish_text": "" if running else finish_text(name, job.state.value, p["total"], issues, processed),
    }


# --------------------------------------------------------------------------- run page


def key_counts(raw: dict[str, int]) -> dict[str, int]:
    out = {k: 0 for k in STATUS_KEYS}
    for st, n in raw.items():
        out[status_key(st)] = out.get(status_key(st), 0) + int(n)
    return out


def resrow_view(ctx: ServerContext, job: Job | None, run: RunView) -> dict[str, Any]:
    app = ctx.store.app()
    machine = (job.machine if job is not None else None) or ctx.machine([run.source_root, run.output_root or ""])
    b = resolve_budget(app, machine)
    return {"choice": app["processor_use"], "cores": min(machine.cores, int(app["processor_cores"])),
            "B": b.B, "M": b.M, "T": b.T, "machine_cores": machine.cores}


_plan_paths: dict[str, dict[int, str]] = {}


def _item_paths(job: Job) -> dict[int, str]:
    if job.plan is None:
        return {}
    cached = _plan_paths.get(job.id)
    if cached is None:
        cached = {it.n: it.source_path for it in job.plan.items}
        _plan_paths.clear()
        _plan_paths[job.id] = cached
    return cached


def live_view(ctx: ServerContext, run: RunView) -> dict[str, Any]:
    job = run.job
    assert job is not None
    p = job.progress()
    paths = _item_paths(job)
    items = []
    for x in p["in_progress"][:IN_PROGRESS_SHOWN]:
        n = x.get("item")
        items.append({"category": x.get("category") or "other", "action": x.get("action") or "",
                      "path": paths.get(n, x.get("label", "")) if n is not None else x.get("label", "")})
    return {
        "state": p["state"], "pct": int(p["percent"]), "done": p["done"], "total": p["total"],
        "elapsed": elapsed(p["elapsed_s"]), "scanned": p["scanned"], "prepare_note": p["prepare_note"],
        "cancel_requested": p["cancel_requested"], "in_flight": p["in_flight"], "in_progress": items,
        "in_progress_count": len(p["in_progress"]), "counts": p["counts"],
    }


def stopped_view(run: RunView) -> dict[str, str]:
    reason = (run.stop_reason or "").strip()
    if "output folder is no longer available" in reason:
        return {"title": "The run stopped: the output drive was disconnected",
                "resume": "Reconnect the drive and start the same run again; completed files are skipped."}
    text = reason.rstrip(".") or "Baleen stopped before the end"
    return {"title": f"The run stopped: {text[0].lower() + text[1:]}",
            "resume": "Start the same run again; completed files are skipped."}


def ordered_codes(reason: str) -> list[str]:
    cs = codes(reason)
    return [c for c in cs if not is_note(c)] + [c for c in cs if is_note(c)]


def results_view(ctx: ServerContext, run: RunView, status: str | None, q: str, page: int) -> dict[str, Any]:
    journal = journal_of(ctx, run)
    statuses = [KEY_TO_STATUS[status].value] if status in KEY_TO_STATUS else None
    rows: list[Any] = []
    total = 0
    counts = dict(run.counts)
    if journal is not None:
        try:
            rows, total = journal.query(statuses=statuses, q=q, page=page, page_size=PAGE_SIZE)
            by_status = journal.counts()["by_status"]
            if by_status:
                counts = by_status
        except Exception:
            rows, total = [], 0
    first = (page - 1) * PAGE_SIZE + 1
    wf = run.wf
    show_output = ("output" in wf.result_columns) if wf else bool(run.output_root)
    return {"rows": rows, "total": total, "first": first, "last": first + len(rows) - 1, "page": page,
            "filter": status if status in KEY_TO_STATUS else None, "q": q, "show_output": show_output,
            "kc": key_counts(counts), "counts": counts}


def inspector_view(run: RunView, r: Any) -> dict[str, Any]:
    src = r.source_path or ""
    name = src.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    folder = (src.split("#", 1)[0] + " → attachment") if "#" in src else split_path(src)[0]
    key = status_key(r.status)
    cs = ordered_codes(r.reason)
    main = [c for c in cs if not is_note(c)]
    helps = [reason_help(c) for c in main if reason_help(c)] if key not in ("ok", "ignored") else []
    checks = []
    for c in parse_checks(r.checks):
        icon, cls, label = CHECK_STATE.get(c.state.value, ("st-skipped", "na", c.state.value))
        checks.append({"id": c.check, "icon": icon, "cls": cls, "label": label, "detail": c.detail})
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    w.writerow(r.csv_row())
    return {"name": name, "dir": folder, "codes": [{"code": c, "label": reason_label(c)} for c in cs],
            "helps": helps, "checks": checks, "row_text": buf.getvalue().rstrip("\n"),
            "can_reveal_source": bool(run.source_root)}


# --------------------------------------------------------------------------- history (UI-H1)


def runs_list(ctx: ServerContext, wf_filter: str, q: str) -> list[dict[str, Any]]:
    out = []
    ql = q.casefold().strip()
    live = current_job(ctx)
    for e in ctx.engine.index.load()[:RUN_INDEX_LIMIT]:
        if wf_filter and e.workflow != wf_filter:
            continue
        if ql and ql not in (e.source_root or "").casefold() and ql not in (e.output_root or "").casefold():
            continue
        wf = get_workflow(e.workflow)
        counts = e.counts
        state = e.state
        total = e.total
        if live is not None and live.id == e.id:
            counts, total, state = dict(live.counts), live.total, "running"
        elif state == "running":
            state = "stopped"
        kc = key_counts(counts)
        issues = kc["failed"] + kc["review"] + kc["unsupported"]
        stack = [{"key": k, "n": kc[k], "pct": (kc[k] / total * 100) if total else 0}
                 for k in ("ok", "review", "failed", "unsupported")]
        out.append({
            "id": e.id, "started_at": e.started_at, "name": wf.name if wf else e.workflow.capitalize(),
            "icon": wf.icon if wf else "layers", "source_root": e.source_root, "total": total, "state": state,
            "issues": issues, "stack": stack,
            "duration": "—" if state == "running" else duration(span(e.started_at, e.finished_at)),
        })
    return out


def tools_view(ctx: ServerContext, *, force: bool = False) -> dict[str, Any]:
    info = ctx.tools.detect(force=force)
    tools = []
    for k in TOOL_KEYS:
        t = info[k]
        spec = SPECS[k]
        tools.append({"key": k, "name": t.name, "role": t.role, "found": t.found, "version": t.version,
                      "source": t.source, "path": t.path or "", "error": t.error, "affects": spec.affects,
                      "fix": spec.fix, "env": spec.env, "override": not t.found and spec.env in (t.error or "")})
    return {"tools": tools, "missing": sum(1 for t in tools if not t["found"])}

