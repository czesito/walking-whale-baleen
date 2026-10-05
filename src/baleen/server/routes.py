"""HTTP routes (spec Appendix B). Pages are server-rendered; htmx swaps fragments. Every POST
passes the SEC-5 origin check in the middleware; GET routes never change state."""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import osutil
from ..paths import is_within, join_rel, long_path
from ..report import report_name
from ..runner import RootError, RunSpec
from ..settings import FOLDER_KEYS, find_field
from ..tools import library_versions
from ..workflows import Workflow, all_workflows
from ..workflows import get as get_workflow
from . import pickers, views
from .context import ServerContext
from .present import file_manager, short_home, status_key
from .runviews import RunView, current_job, find_run, journal_of
from .security import same, session_cookie_header

log = logging.getLogger("baleen.server")

PROJECT_URL = "https://github.com/czesito/walking-whale-baleen"


# --------------------------------------------------------------------------- helpers


def _ctx(request: Request) -> ServerContext:
    return request.app.state.ctx


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def trigger(after_settle: bool = False, **events: Any) -> dict[str, str]:
    """HX-Trigger header for baleen.js (toast, modal-open, ...). Events on a response with no swap
    (204) must use the plain header; after_settle waits until swapped content is in place."""
    name = "HX-Trigger-After-Settle" if after_settle else "HX-Trigger"
    return {name: json.dumps({f"baleen:{k.replace('_', '-')}": v for k, v in events.items()})}


def render(ctx: ServerContext, name: str, *, status: int = 200, headers: dict[str, str] | None = None,
           background: BackgroundTask | None = None, **kw: Any) -> HTMLResponse:
    body = ctx.templates.get_template(name).render(**kw)
    return HTMLResponse(body, status_code=status, headers=headers, background=background)


def render_str(ctx: ServerContext, name: str, **kw: Any) -> str:
    return ctx.templates.get_template(name).render(**kw)


def shell(ctx: ServerContext, *, active: str, page: str, wide: bool = False, rail_poll: bool = True,
          **kw: Any) -> dict[str, Any]:
    """Context every page in the shell needs: rail, running-job card, title, preferences."""
    prefs = ctx.store.app()
    job = current_job(ctx)
    rj = views.railjob_view(job, poll=rail_poll)
    page_title = f"{page} · Baleen"
    title = page_title
    if rj["running"] and prefs.get("progress_in_title", True):
        title = f"{rj['pct']}% · {rj['wf_name']} · Baleen"
    return {
        "workflows": all_workflows(), "active": active, "runs_count": len(ctx.engine.index.load()),
        "tools_ok": not ctx.tools.missing(), "rj": rj, "prefs": prefs, "picker": ctx.picker is not None,
        "title": title, "page_title": page_title, "wide": wide, **kw,
    }


def not_found(ctx: ServerContext, request: Request, message: str = "This page doesn't exist",
              detail: str = "The address may be from an older Baleen, or mistyped.") -> Response:
    if is_htmx(request):
        return Response("Not found.", status_code=404, media_type="text/plain")
    return render(ctx, "notfound.html", status=404, message=message, detail=detail,
                  **shell(ctx, active="", page="Not found"))


async def form_dict(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


# --------------------------------------------------------------------------- auth & home


async def auth(request: Request) -> Response:
    """SEC-2: exchange the launch token for the session cookie, then go to /."""
    ctx = _ctx(request)
    if not same(request.query_params.get("t"), ctx.secrets.token):
        return HTMLResponse(render_str(ctx, "forbidden.html", title="Baleen"), status_code=403)
    resp = RedirectResponse("/", status_code=303)
    resp.headers.append("Set-Cookie", session_cookie_header(ctx.secrets.session))
    return resp


async def home(request: Request) -> Response:
    return RedirectResponse(all_workflows()[0].route, status_code=303)


async def favicon(request: Request) -> Response:
    return RedirectResponse(f"/static/favicon.svg?v={_ctx(request).version}", status_code=307)


# --------------------------------------------------------------------------- workflow pages


def setup_context(ctx: ServerContext, wf: Workflow, *, values: dict[str, str] | None = None,
                  announce: bool = False, start_error: str = "") -> dict[str, Any]:
    folders = values if values is not None else ctx.folder_values(wf)
    statuses = views.validate_fields(wf, folders)
    options = ctx.option_values(wf)
    modified = any(options.get(f.key) != f.default for g in wf.options for f in g.fields)
    prv = views.preview_view(ctx, wf, statuses, announce=announce) if wf.preview else None
    steps = {"options": 2, "preview": 3 if wf.options else 2}
    return {
        "wf": wf, "folders": folders, "statuses": statuses, "options": options, "modified": modified,
        "prv": prv, "bar": views.actionbar_view(ctx, wf, statuses, prv), "steps": steps,
        "missing_tools": views.tool_notices(ctx, wf.mode), "running_job": current_job(ctx),
        "start_error": start_error, "picker": ctx.picker is not None,
    }


async def workflow_page(request: Request) -> Response:
    ctx = _ctx(request)
    wf = get_workflow(request.path_params["wf_id"])
    if wf is None:
        return not_found(ctx, request)
    sc = setup_context(ctx, wf)
    base = shell(ctx, active=wf.id, page=wf.name)
    base.update(sc)
    return render(ctx, "workflow.html", **base)


def folder_fragments(ctx: ServerContext, wf: Workflow, sc: dict[str, Any], *, main_key: str | None,
                     preview: bool = True) -> str:
    """Status lines (one main, the rest out of band), the action bar and the preview card."""
    macros = ctx.templates.get_template("partials/folder_field.html").module
    parts = []
    if main_key is not None:
        parts.append(str(macros.msg_line(main_key, sc["statuses"].get(main_key))))
    for f in wf.folders:
        if f.key != main_key:
            parts.append(str(macros.msg_line(f.key, sc["statuses"].get(f.key), True)))
    parts.append(render_str(ctx, "_actionbar.html", oob=True, **sc))
    if wf.preview and preview:
        parts.append(render_str(ctx, "_preview.html", oob=True, **sc))
    parts.append(render_str(ctx, "_setup_notices.html", oob=True, **sc))
    return "".join(parts)


def save_folders(ctx: ServerContext, wf: Workflow, form: dict[str, str]) -> dict[str, str]:
    """Folder values from the request (pasted paths), saved as 'last used' when absolute (§11)."""
    current = ctx.folder_values(wf)
    values = {f.key: views.clean_path(form[f.key]) if f.key in form else current[f.key] for f in wf.folders}
    changes = {k: v for k, v in values.items() if v != current[k] and (not v or os.path.isabs(v))}
    if changes:
        ctx.store.update(changes)
    return values


async def api_validate_path(request: Request) -> Response:
    """UI-C1: inline folder validation on blur; returns the field's status line (+ OOB parts)."""
    ctx = _ctx(request)
    form = await form_dict(request)
    wf = get_workflow(form.get("wf", ""))
    key = form.get("field", "")
    if wf is None or key not in wf.folder_keys():
        return Response("Unknown field.", status_code=400, media_type="text/plain")
    values = await run_in_threadpool(save_folders, ctx, wf, form)
    sc = await run_in_threadpool(setup_context, ctx, wf, values=values)
    if not is_htmx(request):
        return JSONResponse({k: v for k, v in sc["statuses"].items()})
    return HTMLResponse(folder_fragments(ctx, wf, sc, main_key=key))


async def api_pick_folder(request: Request) -> Response:
    """§12.9: open the native folder dialog; return the path or "cancelled" (10 min timeout)."""
    ctx = _ctx(request)
    if ctx.picker is None:
        return Response("No folder dialog on this computer.", status_code=404, media_type="text/plain")
    form = await form_dict(request)
    key = form.get("field", "")
    wf = get_workflow(form.get("wf", "")) if key != "work_dir" else None
    if key != "work_dir" and (wf is None or key not in wf.folder_keys()):
        return Response("Unknown field.", status_code=400, media_type="text/plain")
    initial = views.clean_path(form.get(key, "")) or None
    result = await run_in_threadpool(pickers.pick_folder, ctx.picker, initial, tmp_dir=str(ctx.home.tmp_dir))
    if not is_htmx(request):
        return JSONResponse({"path": result.path} if result.path else {"cancelled": True, "error": result.error})
    if result.path is None and result.cancelled:
        return Response(status_code=204)  # UI-C6: cancelling the dialog changes nothing
    if key == "work_dir":
        if result.path is None:
            return render(ctx, "_resource.html", res=views.resource_view(ctx, work_error=result.error),
                          picker=True)
        return await run_in_threadpool(_save_work_dir, ctx, result.path)
    assert wf is not None
    if result.path is None:
        macros = ctx.templates.get_template("partials/folder_field.html").module
        return HTMLResponse(str(macros.msg_line(key, {"error": result.error})))
    form[key] = result.path
    values = await run_in_threadpool(save_folders, ctx, wf, form)
    sc = await run_in_threadpool(setup_context, ctx, wf, values=values)
    return HTMLResponse(folder_fragments(ctx, wf, sc, main_key=key),
                        headers=trigger(set_value={"id": f"f-{key}", "value": result.path}))


def _settings_for(ctx: ServerContext, values: dict[str, str]) -> dict[str, Any]:
    s = copy.deepcopy(ctx.store.snapshot())
    s["workflow"].update(values)
    return s


async def api_preview(request: Request) -> Response:
    """UI-C4: scan and plan without writing anything; the card polls while it runs."""
    ctx = _ctx(request)
    form = await form_dict(request)
    wf = get_workflow(form.get("wf", ""))
    if wf is None or not wf.preview:
        return Response("Unknown workflow.", status_code=400, media_type="text/plain")
    if form.get("poll") == "1":
        sc = setup_context(ctx, wf, announce=True)
        parts = [render_str(ctx, "_preview.html", **sc)]
        if sc["prv"]["state"] != "running":
            parts.append(render_str(ctx, "_actionbar.html", oob=True, **sc))
        return HTMLResponse("".join(parts))
    values = await run_in_threadpool(save_folders, ctx, wf, form)
    statuses = await run_in_threadpool(views.validate_fields, wf, values)
    if views.ready(statuses):
        source, output = views.roots_for(wf, values)
        ctx.previews.start(wf.id, ctx.preview_key(wf), wf.mode, source, output, _settings_for(ctx, values))
    sc = await run_in_threadpool(setup_context, ctx, wf, values=values)
    return HTMLResponse(render_str(ctx, "_preview.html", **sc)
                        + folder_fragments(ctx, wf, sc, main_key=None, preview=False))


async def api_start(request: Request) -> Response:
    """UI-C5 / UI-K2: validate, confirm (when the workflow asks), start, go to the run page."""
    ctx = _ctx(request)
    wf = get_workflow(request.path_params["wf_id"])
    if wf is None:
        return Response("Unknown workflow.", status_code=404, media_type="text/plain")
    form = await form_dict(request)
    values = await run_in_threadpool(save_folders, ctx, wf, form)
    sc = await run_in_threadpool(setup_context, ctx, wf, values=values)
    no_swap = {"HX-Reswap": "none"}
    if not views.ready(sc["statuses"]):
        return HTMLResponse(folder_fragments(ctx, wf, sc, main_key=None), headers=no_swap)
    confirmed = form.get("confirmed") == "1"
    if wf.confirm and not confirmed:
        return render(ctx, "_confirm.html", headers=trigger(True, modal_open=True),
                      **confirm_context(ctx, wf, values))
    source, output = views.roots_for(wf, values)
    spec = RunSpec(wf.mode, source, output, _settings_for(ctx, values))
    try:
        job = await run_in_threadpool(wf.run, ctx.engine, spec) if wf.run else await run_in_threadpool(
            ctx.engine.start, spec)
    except RootError as e:
        if confirmed:
            return render(ctx, "_confirm.html", error=str(e), **confirm_context(ctx, wf, values))
        sc = setup_context(ctx, wf, values=values, start_error=str(e))
        return HTMLResponse(render_str(ctx, "_setup_notices.html", oob=True, **sc), headers=no_swap)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{job.id}"})


def confirm_context(ctx: ServerContext, wf: Workflow, values: dict[str, str]) -> dict[str, Any]:
    route = []
    for f in wf.folders:
        route.append({"label": "From" if f.role == "read" else "To", "value": values.get(f.key, "")})
    counts_line = ""
    if wf.preview:
        prv = views.preview_view(ctx, wf, views.validate_fields(wf, values))
        if prv["state"] == "done":
            c = prv["summary"]["counts"]
            counts_line = f"{c['files']:,} · {c['convert']:,} to convert · {c['copy']:,} to copy"
    settings = ctx.store.workflow()
    return {"wf": wf, "route": route, "counts_line": counts_line, "chips": views.chips_for(wf, settings),
            "error": ""}


# --------------------------------------------------------------------------- settings API


def _save_work_dir(ctx: ServerContext, raw: str) -> HTMLResponse:
    value, err = views.validate_work_dir(ctx, raw)
    if err:
        return render(ctx, "_resource.html", res=views.resource_view(ctx, work_error=err, work_value=raw),
                      picker=ctx.picker is not None)
    ctx.store.update({"work_dir": value})
    return render(ctx, "_resource.html", res=views.resource_view(ctx), picker=ctx.picker is not None)


def _custom_defaults(ctx: ServerContext, changes: dict[str, Any]) -> None:
    """Switching to Custom starts from the value currently in use (prototype behaviour)."""
    app = ctx.store.app()
    from ..scheduler import resolve_budget

    machine = ctx.machine(views.all_folder_paths(ctx))
    b = resolve_budget(app, machine)
    if changes.get("processor_use") == "custom" and "processor_cores" not in changes:
        changes["processor_cores"] = b.B
    if changes.get("memory_limit") == "custom" and "memory_gb" not in changes:
        changes["memory_gb"] = b.M
    if changes.get("transfer_slots") == "custom" and "transfer_count" not in changes:
        changes["transfer_count"] = b.T
    if "processor_cores" in changes:
        changes["processor_cores"] = min(machine.cores, max(1, int(changes["processor_cores"])))
    if "memory_gb" in changes:
        changes["memory_gb"] = min(max(1, machine.ram_gb - 2), max(1, int(changes["memory_gb"])))
    if "transfer_count" in changes:
        changes["transfer_count"] = min(16, max(1, int(changes["transfer_count"])))


async def api_settings(request: Request) -> Response:
    """Save workflow settings or app preferences (incl. resource use; applies live, §11)."""
    ctx = _ctx(request)
    form = await form_dict(request)
    ui = form.pop("ui", "")
    reset = form.pop("reset", "")
    step = form.pop("step", "")
    form.pop("wf", None)
    changes: dict[str, Any] = {}
    if step:
        # Steppers send relative steps ("processor_cores:1"), so rapid presses all count (UI-S2).
        key, _, delta = step.partition(":")
        if key not in ("processor_cores", "transfer_count"):
            return Response("Unknown step.", status_code=400, media_type="text/plain")
        try:
            changes[key] = int(ctx.store.app()[key]) + int(delta)
        except ValueError:
            return Response("Bad step.", status_code=400, media_type="text/plain")
    for k, v in form.items():
        try:
            find_field(k)
        except KeyError:
            return Response(f"Unknown setting {k}.", status_code=400, media_type="text/plain")
        if k in FOLDER_KEYS:
            v = views.clean_path(v)
        changes[k] = v
    if "work_dir" in changes:
        raw = changes.pop("work_dir")
        if changes or reset:
            return Response("work_dir is saved on its own.", status_code=400, media_type="text/plain")
        return await run_in_threadpool(_save_work_dir, ctx, raw)
    try:
        await run_in_threadpool(_custom_defaults, ctx, changes)
        if reset == "workflow":
            await run_in_threadpool(ctx.store.reset_workflow)
        elif reset == "resources":
            await run_in_threadpool(ctx.store.reset_resources)
        elif reset:
            return Response("Unknown reset.", status_code=400, media_type="text/plain")
        if changes:
            await run_in_threadpool(ctx.store.update, changes)
    except (ValueError, TypeError) as e:
        return Response(str(e), status_code=400, media_type="text/plain")

    if ui.startswith("options:"):
        wf = get_workflow(ui.split(":", 1)[1])
        if wf is None:
            return Response(status_code=204)
        sc = setup_context(ctx, wf)
        parts = [render_str(ctx, "_options.html", **sc), render_str(ctx, "_actionbar.html", oob=True, **sc)]
        if wf.preview:
            parts.append(render_str(ctx, "_preview.html", oob=True, **sc))
        return HTMLResponse("".join(parts))
    if ui == "plan":  # the memory slider: save, then refresh only the summary (the slider keeps focus)
        machine = ctx.machine(views.all_folder_paths(ctx))
        return render(ctx, "_plan.html", plan=views.plan_view(ctx.store.app(), machine))
    if ui == "resource":
        headers = trigger(toast="Resource use reset to defaults") if reset == "resources" else None
        return render(ctx, "_resource.html", headers=headers, res=views.resource_view(ctx),
                      picker=ctx.picker is not None)
    if ui.startswith("runres:"):
        run = find_run(ctx, ui.split(":", 1)[1])
        if run is None:
            return Response(status_code=204)
        return render(ctx, "_resrow.html", headers=trigger(toast="Applies to new tasks right away"), run=run,
                      res=views.resrow_view(ctx, run.job, run))
    if ui == "prefs":
        prefs = ctx.store.app()
        return render(ctx, "_prefs.html", prefs=prefs, headers=trigger(prefs={
            "notify": bool(prefs["notify_on_finish"]), "title": bool(prefs["progress_in_title"])}))
    if ui == "reset":
        return Response(status_code=204, headers=trigger(toast="Options reset", modal_close=True))
    return Response(status_code=204)


async def settings_plan(request: Request) -> Response:
    """UI-S2 "With these settings": computed on the server with the §5.5 rules."""
    ctx = _ctx(request)
    app = views.plan_overrides(ctx.store.app(), dict(request.query_params))
    machine = ctx.machine(views.all_folder_paths(ctx))
    return render(ctx, "_plan.html", plan=views.plan_view(app, machine))


# --------------------------------------------------------------------------- run pages


def run_context(ctx: ServerContext, run: RunView, *, status: str | None = None, q: str = "", page: int = 1,
                announce_finish: str = "") -> dict[str, Any]:
    kw: dict[str, Any] = {"run": run, "chips": views.chips_for(run.wf, run.settings),
                          "report_exists": (not run.live) and os.path.exists(long_path(run.report_path)),
                          "log_path": short_home(str(ctx.home.logs_dir / "baleen.log"), str(ctx.home.root)),
                          "stopped": views.stopped_view(run), "announce_finish": announce_finish}
    if run.live:
        live = views.live_view(ctx, run)
        journal = run.job.journal if run.job else None
        kw.update(live=live, kc=views.key_counts(live["counts"]), latest=journal.latest(6) if journal else [],
                  res=views.resrow_view(ctx, run.job, run), announce_start=live["done"] == 0)
    else:
        kw.update(views.results_view(ctx, run, status, q, page))
    return kw


def _page_arg(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return 1


async def run_page(request: Request) -> Response:
    ctx = _ctx(request)
    run = await run_in_threadpool(find_run, ctx, request.path_params["run_id"])
    if run is None:
        return not_found(ctx, request, "This run isn't in the history",
                         "Only the 100 most recent runs are listed. Its report is still in its folder.")
    status = request.query_params.get("status") or None
    kw = await run_in_threadpool(run_context, ctx, run, status=status, q=request.query_params.get("q", ""),
                                 page=_page_arg(request))
    base = shell(ctx, active="runs", page="Run", wide=True, rail_poll=not run.live)
    base.update(kw)
    return render(ctx, "run.html", **base)


async def run_progress(request: Request) -> Response:
    """Polled while running; the final response swaps in the results (no further polling)."""
    ctx = _ctx(request)
    run = await run_in_threadpool(find_run, ctx, request.path_params["run_id"])
    if run is None:
        return Response("Not found.", status_code=404, media_type="text/plain")
    job = run.job
    if run.live:
        kw = await run_in_threadpool(run_context, ctx, run)
        rj = views.railjob_view(job, poll=False)
        return render(ctx, "_progress_oob.html", rj=rj, **kw)
    issues_text = (f"{run.issues:,} of {run.total:,} files need your attention" if run.issues
                   else f"All {run.total:,} files are OK")
    kw = await run_in_threadpool(run_context, ctx, run, announce_finish=f"Run finished · {issues_text}")
    return render(ctx, "_final_oob.html", rj=views.railjob_view(job, poll=False), **kw)


async def run_rows(request: Request) -> Response:
    """Results table fragment (UI-R4): filter, text filter, page. Updates the tiles out of band."""
    ctx = _ctx(request)
    run = await run_in_threadpool(find_run, ctx, request.path_params["run_id"])
    if run is None or run.live:
        return Response("Not found.", status_code=404, media_type="text/plain")
    status = request.query_params.get("status") or None
    q = request.query_params.get("q", "")
    page = _page_arg(request)
    kw = await run_in_threadpool(run_context, ctx, run, status=status, q=q, page=page)
    from .templating import qs

    query = qs(status=kw["filter"], q=q, page=page)
    push = f"/runs/{run.id}" + (f"?{query}" if query else "")
    body = render_str(ctx, "_rows.html", **kw) + render_str(ctx, "_tiles.html", oob=True, tiles_interactive=True,
                                                             **kw)
    return HTMLResponse(body, headers={"HX-Push-Url": push})


async def run_row(request: Request) -> Response:
    """Inspector fragment for one row (UI-R5)."""
    ctx = _ctx(request)
    run = await run_in_threadpool(find_run, ctx, request.path_params["run_id"])
    if run is None:
        return Response("Not found.", status_code=404, media_type="text/plain")
    journal = journal_of(ctx, run)
    r = journal.get(request.path_params["n"]) if journal else None
    if r is None:
        return Response("Not found.", status_code=404, media_type="text/plain")
    return render(ctx, "_inspector.html", run=run, r=r, key=status_key(r.status), i=views.inspector_view(run, r))


async def run_report(request: Request) -> Response:
    ctx = _ctx(request)
    run = await run_in_threadpool(find_run, ctx, request.path_params["run_id"])
    if run is None or run.live or not os.path.isfile(long_path(run.report_path)):
        return Response("Not found.", status_code=404, media_type="text/plain")
    return FileResponse(long_path(run.report_path), media_type="text/csv; charset=utf-8",
                        filename=report_name(run.id))


async def job_status(request: Request) -> Response:
    """The rail's running-job card (UI-G2), polled every 2 s while a job runs."""
    ctx = _ctx(request)
    job = ctx.engine.current() or ctx.engine.last()
    return render(ctx, "_railjob.html", rj=views.railjob_view(job))


async def api_cancel(request: Request) -> Response:
    """Cancel (§5.6): stop dispatching; files in progress finish; the rest become Skipped."""
    ctx = _ctx(request)
    await run_in_threadpool(ctx.engine.cancel)
    return Response(status_code=204, headers=trigger(modal_close=True, poll_now=True))


# --------------------------------------------------------------------------- history


async def runs_page(request: Request) -> Response:
    ctx = _ctx(request)
    wf_filter = request.query_params.get("wf", "")
    q = request.query_params.get("q", "")
    if wf_filter and get_workflow(wf_filter) is None:
        wf_filter = ""
    runs = await run_in_threadpool(views.runs_list, ctx, wf_filter, q)
    kw = {"runs": runs, "wf_filter": wf_filter, "q": q,
          "wf_filters": [("", "All"), *[(w.id, w.name) for w in all_workflows()]]}
    if is_htmx(request) and request.headers.get("hx-target") == "runs-table":
        return render(ctx, "_runs_table.html", **kw)
    base = shell(ctx, active="runs", page="Runs", wide=True)
    base.update(kw, any_runs=bool(ctx.engine.index.load()))
    return render(ctx, "runs.html", **base)


# --------------------------------------------------------------------------- system pages


async def tools_page(request: Request) -> Response:
    ctx = _ctx(request)
    tv = await run_in_threadpool(views.tools_view, ctx)
    base = shell(ctx, active="tools", page="Tools")
    base.update(tv)
    return render(ctx, "tools.html", **base)


async def api_tools_recheck(request: Request) -> Response:
    ctx = _ctx(request)
    tv = await run_in_threadpool(views.tools_view, ctx, force=True)
    return render(ctx, "_tools_list.html", headers=trigger(toast="All tools re-checked"), oob_dot=True,
                  tools_ok=tv["missing"] == 0, **tv)


async def settings_page(request: Request) -> Response:
    ctx = _ctx(request)
    res = await run_in_threadpool(views.resource_view, ctx)
    overrides = [(k, os.environ[k]) for k in ("BALEEN_SOFFICE", "BALEEN_FFMPEG", "BALEEN_FFPROBE",
                                              "BALEEN_JAVA_HOME", "BALEEN_VERAPDF") if os.environ.get(k)]
    base = shell(ctx, active="settings", page="Settings")
    base.update(res=res, home_path=str(ctx.home.root), address=ctx.origin, overrides=overrides,
                adv=ctx.store.advanced(),
                log_short=short_home(str(ctx.home.logs_dir / "baleen.log"), str(ctx.home.root)))
    return render(ctx, "settings.html", **base)


async def about_page(request: Request) -> Response:
    ctx = _ctx(request)
    tools = ctx.tools.detect()
    libs = library_versions()

    def ver(key: str, prefix: str = "") -> str:
        t = tools.get(key)
        return f" {prefix}{t.version}" if t and t.found and t.version else ""

    py = libs.get("python", {}).get("version", "")
    third = [
        (f"LibreOffice{ver('libreoffice')}", "MPL-2.0"),
        (f"FFmpeg{ver('ffmpeg')} (GPL build)", "GPL-2.0-or-later"),
        (f"Eclipse Temurin{ver('java').replace('Temurin ', '')}", "GPL-2.0 + CE"),
        (f"veraPDF{ver('verapdf')}", "GPL-3.0+ / MPL-2.0"),
        (f"CPython {'.'.join(py.split('.')[:2])}" if py else "CPython", "PSF-2.0"),
        ("Pillow", "MIT-CMU"), ("pypdf", "BSD-3-Clause"), ("Starlette · uvicorn", "BSD-3-Clause"),
        ("Jinja2", "BSD-3-Clause"), ("htmx 2", "0BSD"), ("Lucide icons", "ISC"),
        ("Inter · IBM Plex Mono · Cormorant Garamond", "OFL-1.1"),
    ]
    links = {"project": PROJECT_URL, "spec": f"{PROJECT_URL}/blob/main/docs/baleen-spec.html",
             "issues": f"{PROJECT_URL}/issues"}
    base = shell(ctx, active="about", page="About")
    base.update(third_party=third, links=links)
    return render(ctx, "about.html", **base)


# --------------------------------------------------------------------------- open & quit


def _open_target(ctx: ServerContext, form: dict[str, str]) -> tuple[str | None, list[str], int]:
    """SEC-7: the path comes from Baleen's own records, never from the client, and must lie inside
    a known run's source / output root or BALEEN_HOME/data."""
    what = form.get("what", "")
    data = str(ctx.home.data_dir)
    if what == "data":
        return data, [data], 200
    if what == "logs":
        return str(ctx.home.logs_dir), [data], 200
    run = find_run(ctx, form.get("run", ""))
    if run is None:
        return None, [], 404
    roots = [p for p in (run.source_root, run.output_root, data) if p]
    if what == "output_root":
        return run.output_root, roots, 200
    if what == "report":
        return run.report_path, roots, 200
    if what in ("source", "output"):
        journal = journal_of(ctx, run)
        try:
            n = int(form.get("n", ""))
        except ValueError:
            return None, roots, 400
        r = journal.get(n) if journal else None
        if r is None:
            return None, roots, 404
        if what == "source":
            if not run.source_root:
                return None, roots, 404
            return join_rel(run.source_root, r.source_path.split("#", 1)[0]), roots, 200
        if not (run.output_root and r.output_path):
            return None, roots, 404
        return join_rel(run.output_root, r.output_path), roots, 200
    return None, roots, 400


async def api_open(request: Request) -> Response:
    """Open / reveal a path in Finder or Explorer (SEC-7)."""
    ctx = _ctx(request)
    form = await form_dict(request)
    path, roots, code = await run_in_threadpool(_open_target, ctx, form)
    if path is None:
        return Response(status_code=code)
    if not os.path.isabs(path) or not any(is_within(path, root) for root in roots):
        return Response("Not allowed.", status_code=403, media_type="text/plain")
    if not os.path.exists(long_path(path)):
        return Response("Not found.", status_code=404, media_type="text/plain")
    is_file = os.path.isfile(long_path(path))
    ok = await run_in_threadpool(osutil.reveal, path)
    if not ok:
        return Response(status_code=500)
    verb = "Revealed in" if is_file else "Opened in"
    return Response(status_code=204, headers=trigger(toast=f"{verb} {file_manager()}"))


async def api_quit(request: Request) -> Response:
    """UI-G4: finish the files in progress, write the report, then /stopped and exit."""
    ctx = _ctx(request)
    await run_in_threadpool(ctx.quit.request, ctx.engine)
    return Response(status_code=204, headers={"HX-Redirect": "/stopped"})


async def stopped_page(request: Request) -> Response:
    ctx = _ctx(request)
    if not ctx.quit.ready.is_set():
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render_str(ctx, "stopped.html", title="Baleen has stopped"),
                        background=BackgroundTask(ctx.quit.stopped_served))


# --------------------------------------------------------------------------- table


def routes() -> list[Route]:
    return [
        Route("/auth", auth, methods=["GET"]),
        Route("/", home, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/job", job_status, methods=["GET"]),
        Route("/runs", runs_page, methods=["GET"]),
        Route("/runs/{run_id}", run_page, methods=["GET"]),
        Route("/runs/{run_id}/progress", run_progress, methods=["GET"]),
        Route("/runs/{run_id}/rows", run_rows, methods=["GET"]),
        Route("/runs/{run_id}/rows/{n:int}", run_row, methods=["GET"]),
        Route("/runs/{run_id}/report.csv", run_report, methods=["GET"]),
        Route("/tools", tools_page, methods=["GET"]),
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings/plan", settings_plan, methods=["GET"]),
        Route("/about", about_page, methods=["GET"]),
        Route("/stopped", stopped_page, methods=["GET"]),
        Route("/api/settings", api_settings, methods=["POST"]),
        Route("/api/validate-path", api_validate_path, methods=["POST"]),
        Route("/api/pick-folder", api_pick_folder, methods=["POST"]),
        Route("/api/preview", api_preview, methods=["POST"]),
        Route("/api/job/cancel", api_cancel, methods=["POST"]),
        Route("/api/tools/recheck", api_tools_recheck, methods=["POST"]),
        Route("/api/open", api_open, methods=["POST"]),
        Route("/api/quit", api_quit, methods=["POST"]),
        Route("/api/{wf_id}", api_start, methods=["POST"]),
        Route("/{wf_id}", workflow_page, methods=["GET"]),
    ]

