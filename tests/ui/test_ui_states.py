"""Every screen and state of design §07 (W-01…W-23), reached with real behaviour in Chromium, each
screenshotted next to the prototype's same state (BALEEN_UI_SHOTS). The tests run in order and
share one server, like a user working through a day: setup, preview, a run, its results, then
cancelled and stopped runs, history, system pages, the narrow drawer, quit."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from . import fake_routes, harness
from .conftest import PROTOTYPE, route_prototype_fonts

pytestmark = pytest.mark.ui

VIEW = {"width": 1440, "height": 900}
NARROW = {"width": 900, "height": 700}
PORT = 8785


@dataclass
class World:
    h: harness.Harness
    browser: Any
    context: Any
    page: Any
    tmp: Path
    arch: Path
    out: Path
    order: list[str]
    errors: list[str] = field(default_factory=list)
    runs: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module")
def w(browser, mpm, tmp_path_factory) -> World:  # noqa: ANN001
    tmp = tmp_path_factory.mktemp("states")
    h = harness.start(tmp, PORT, mpm)
    arch = tmp / "Volumes" / "Archive" / "04 Events (2000–2005)"
    order = harness.make_archive(arch)
    harness.hold_files(arch, order, 812, 4)
    out = tmp / "Users" / "mia" / "Baleen output" / "04 Events"
    context = browser.new_context(viewport=VIEW, reduced_motion="reduce")
    page = context.new_page()
    world = World(h, browser, context, page, tmp, arch, out, order)
    page.on("pageerror", lambda e: world.errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: world.errors.append(f"console: {m.text} @ {m.location}")
            if m.type == "error" else None)
    yield world
    fake_routes.GATE.set()
    context.close()
    h.stop()


def shot(w: World, name: str, demo: str | None, *, viewport: dict[str, int] = VIEW, page: Any = None,
         proto_steps: Any = None) -> None:
    """app-<name>.png now, and proto-<name>.png from the prototype's same state and viewport."""
    d = harness.ui_shots_dir()
    if d is None:
        return
    (page or w.page).screenshot(path=str(d / f"app-{name}.png"), caret="initial")
    if demo is None:
        return
    ctx = w.browser.new_context(viewport=viewport, reduced_motion="reduce")
    p = ctx.new_page()
    route_prototype_fonts(p)
    p.goto(PROTOTYPE.as_uri())
    until(p, "window.baleenProto !== undefined")
    p.evaluate("name => window.baleenProto.demo(name)", demo)
    if proto_steps:
        proto_steps(p)
    p.wait_for_timeout(350)
    p.screenshot(path=str(d / f"proto-{name}.png"), caret="initial")
    ctx.close()


def until(page: Any, js: str, timeout: float = 10.0) -> None:
    """Poll a JavaScript expression through page.evaluate (CDP), which the page's CSP does not
    block; Playwright's wait_for_function relies on eval, which the CSP of SEC-6 forbids."""
    deadline = time.monotonic() + timeout
    while True:
        if page.evaluate(js):
            page.wait_for_timeout(60)  # let htmx settle what it just swapped in
            return
        if time.monotonic() > deadline:
            raise TimeoutError(js)
        time.sleep(0.05)


def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        time.sleep(0.05)


def settle(page: Any, ms: int = 250) -> None:
    page.wait_for_timeout(ms)


def ready(page: Any, selector: str) -> None:
    """Wait for a swapped-in element and for htmx to process it: htmx attaches its listeners in the
    settle step (20 ms after the swap), and a test can act faster than any person."""
    page.wait_for_selector(selector)
    settle(page, 120)


def go(w: World, path: str) -> None:
    w.page.goto(w.h.base + path)
    settle(w.page)


def fill_folder(page: Any, key: str, value: str) -> None:
    """Paste a path and leave the field; wait for the new status line (validation on blur)."""
    page.evaluate(f"document.getElementById('f-{key}-msg').dataset.stale = '1'")
    page.fill(f"#f-{key}", value)
    page.press(f"#f-{key}", "Tab")
    until(page, f"document.getElementById('f-{key}-msg').dataset.stale === undefined")
    settle(page, 80)


def wait_results(page: Any, timeout: float = 90_000) -> None:
    page.wait_for_selector("#run-results", timeout=timeout)
    settle(page, 400)


# --------------------------------------------------------------------------- W-21, W-14, W-01


def test_w21_not_authorised(w: World) -> None:
    ctx = w.browser.new_context(viewport=VIEW, reduced_motion="reduce")
    p = ctx.new_page()
    resp = p.goto(w.h.base + "/convert")
    assert resp.status == 403
    assert p.locator("h1").inner_text() == "Open Baleen from its launcher"
    shot(w, "W-21-forbidden", "forbidden", page=p)
    ctx.close()
    w.page.goto(w.h.auth_url)
    assert w.page.url.endswith("/convert")


def test_w14_runs_empty(w: World) -> None:
    go(w, "/runs")
    assert w.page.get_by_text("No runs yet").is_visible()
    assert w.page.locator(".empty a.btn.primary").inner_text().startswith("Start with Convert")
    shot(w, "W-14-runs-empty", "runs-empty")


def test_w01_convert_empty(w: World) -> None:
    go(w, "/convert")
    assert w.page.locator("#ab-start").is_disabled()
    assert w.page.locator("#actionbar .status").inner_text() == "Choose a source and an output folder."
    assert w.page.locator("#rail-convert").get_attribute("aria-current") == "page"
    assert w.page.title() == "Convert · Baleen"
    shot(w, "W-01-convert-empty", "convert-empty")


# --------------------------------------------------------------------------- system pages


def test_w15a_tools_all_found(w: World) -> None:
    go(w, "/tools")
    assert w.page.get_by_text("All 5 tools found").is_visible()
    shot(w, "W-15a-tools", "tools")


def test_w16_settings(w: World) -> None:
    go(w, "/settings")
    plan = w.page.locator("#plan")
    assert "4 file tasks at once" in plan.inner_text()
    assert w.page.locator(".meter-lbl").first.inner_text() == "Uses 4 of 8 cores"
    shot(w, "W-16-settings", "settings")


def test_w23_settings_custom(w: World) -> None:
    page = w.page
    app = w.h.ctx.store.app
    page.click("#res-processor_use-custom")
    wait_for(lambda: app()["processor_use"] == "custom")
    ready(page, "#res-processor_cores-up")
    page.click("#res-processor_cores-up")
    page.click("#res-processor_cores-up")  # quick presses both count
    wait_for(lambda: app()["processor_cores"] == 6)
    until(page, "document.querySelector('.meter-lbl').textContent === 'Uses 6 of 8 cores'")
    page.click("#res-memory_limit-custom")
    wait_for(lambda: app()["memory_limit"] == "custom")
    ready(page, "#res-memory")
    page.focus("#res-memory")  # the slider moves ±1 GB with the arrow keys (design §06)
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    wait_for(lambda: app()["memory_gb"] == 6)
    assert page.evaluate("document.activeElement.id") == "res-memory"
    until(page, "document.getElementById('res-memory-lbl').textContent === '6 GB'")
    page.click("#res-transfer_slots-custom")
    wait_for(lambda: app()["transfer_slots"] == "custom")
    ready(page, "#res-transfer_count-down")
    for n in (3, 2):
        page.click("#res-transfer_count-down")
        wait_for(lambda n=n: app()["transfer_count"] == n)
    until(page, "document.getElementById('plan').textContent.includes('Up to 6 GB')")
    until(page, "document.getElementById('plan').textContent.includes('2 file transfers')")
    text = page.locator("#plan").inner_text()
    assert "6 file tasks at once" in text and "3 document converters" in text
    shot(w, "W-23-settings-custom", "settings-custom")
    page.click("#res-defaults")
    wait_for(lambda: app()["processor_use"] == "balanced" and app()["memory_limit"] == "auto")
    until(page, "document.querySelector('.meter-lbl').textContent === 'Uses 4 of 8 cores'")


def test_w17_about(w: World) -> None:
    go(w, "/about")
    assert w.page.get_by_text("Walking Whale Co., Ltd. · Taipei").is_visible()
    assert w.page.get_by_text("When reporting a problem, never attach archive files.").is_visible()
    shot(w, "W-17-about", "about")


# --------------------------------------------------------------------------- setup: W-12, W-02…W-06


def test_w12_check_setup(w: World) -> None:
    check = w.tmp / "Volumes" / "Archive" / "03 Publications"
    harness.make_check_tree(check)
    go(w, "/check")
    fill_folder(w.page, "check_dir", str(check))
    assert w.page.locator("#f-check_dir-msg").inner_text() == "Readable"
    bar = w.page.locator("#actionbar .status").inner_text()
    assert bar == "Ready to check. Nothing will be written to this folder."
    assert w.page.get_by_text("What Baleen checks").is_visible()
    shot(w, "W-12-check", "check")


def test_w02_folder_error(w: World) -> None:
    go(w, "/convert")
    fill_folder(w.page, "source_dir", str(w.arch))
    fill_folder(w.page, "output_dir", str(w.arch / "converted"))
    msg = w.page.locator("#f-output_dir-msg")
    assert msg.inner_text() == "This folder is inside the source folder. Choose a folder outside it."
    until(w.page, "document.getElementById('f-output_dir-field').classList.contains('error')")
    assert w.page.locator("#f-output_dir").get_attribute("aria-invalid") == "true"
    assert w.page.locator("#ab-start").is_disabled()
    shot(w, "W-02-convert-error", "convert-error")


def test_w03_preview_ready(w: World) -> None:
    page = w.page
    fill_folder(page, "output_dir", str(w.out))
    assert page.locator("#f-output_dir-msg").inner_text().startswith("Writable · ")
    page.click("#ab-preview")
    page.wait_for_selector("#preview-card[data-preview=done]", timeout=30_000)
    settle(page)
    stats = page.locator(".stats").inner_text()
    assert "1,670" in stats
    until(page, "document.getElementById('live').textContent.startsWith('Preview ready: 1,670 files')")
    assert page.get_by_text("Renamed to avoid clashes").is_visible()
    bar = page.locator("#actionbar .status").inner_text()
    assert re.match(r"1,670 files · [\d,]+ to convert · preview up to date", bar)
    shot(w, "W-03-convert-ready", "convert-ready")


def test_w04_preview_stale(w: World) -> None:
    page = w.page
    page.click("#opt-pdfa_level-1b")
    page.wait_for_selector("#preview-card[data-preview=stale]")
    assert page.get_by_text("Preview is out of date").is_visible()
    assert page.locator("#ab-start").is_enabled()
    assert page.locator(".preset option").inner_text() == "Default (modified)"
    shot(w, "W-04-convert-stale", "convert-stale")
    page.locator("#preview-card").scroll_into_view_if_needed()
    shot(w, "W-04-convert-stale-preview-card", None)  # the card itself (below the fold in both)
    page.locator("#scroll").evaluate("el => el.scrollTop = 0")
    page.click("#opt-reset")
    page.wait_for_selector("#preview-card[data-preview=done]")


def test_w05_tool_missing(w: World) -> None:
    w.h.tools.missing_set = {"verapdf"}
    go(w, "/convert")
    assert w.page.get_by_text("veraPDF isn't available").is_visible()
    assert "bad" in (w.page.locator("#rail-tools-dot").get_attribute("class") or "")
    assert w.page.locator("#ab-start").is_enabled()
    shot(w, "W-05-convert-tools", "convert-tools")


def test_w15_tools_missing(w: World) -> None:
    go(w, "/tools")
    assert w.page.get_by_text("1 tool is missing").is_visible()
    assert w.page.locator("#tool-verapdf .badge.failed").inner_text() == "Missing"
    shot(w, "W-15-tools-missing", "tools-missing")
    w.h.tools.missing_set = set()
    w.page.click("#tools-recheck")
    w.page.wait_for_selector("text=All 5 tools found")
    until(w.page, "!document.getElementById('rail-tools-dot').classList.contains('bad')")


def test_w06_confirmation(w: World) -> None:
    go(w, "/convert")
    w.page.click("#ab-start")
    w.page.wait_for_selector("#overlay.open #confirm-start")
    settle(w.page)
    assert w.page.evaluate("document.activeElement.id") == "confirm-start"
    assert w.page.get_by_text("Your source folder will not be changed.").is_visible()
    assert re.match(r"1,670 · [\d,]+ to convert · [\d,]+ to copy", w.page.locator(".route dd.ui-font").inner_text())
    shot(w, "W-06-confirm", "confirm")


# --------------------------------------------------------------------------- the run: W-07, W-20, W-08, W-09


def test_w07_running(w: World) -> None:
    page = w.page
    fake_routes.GATE.clear()
    page.click("#confirm-start")
    page.wait_for_url(re.compile(r"/runs/\d{8}-\d{6}"))
    w.runs["main"] = page.url.rsplit("/", 1)[1]
    w.h.wait_held(4)
    until(page, "document.querySelector('.kick-inprog') && "
                           "document.querySelector('.kick-inprog').textContent === 'In progress (4)'", timeout=15)
    settle(page, 1200)
    assert page.locator("#run-pill").inner_text() == "Running"
    assert page.locator("#latest-list li").count() == 6
    assert re.match(r"\d+% · Convert · Baleen", page.title())
    assert page.locator("#run-processor").input_value() == "balanced"
    assert "Using 4 of 8 cores" in page.locator("#res-row").inner_text()
    shot(w, "W-07-run-running", "run-running")
    # D-23: Processor use applies live, with a toast.
    page.select_option("#run-processor", "gentle")
    page.wait_for_selector("#toast.show", timeout=5000)
    assert page.locator("#toast").inner_text() == "Applies to new tasks right away"
    assert w.h.ctx.store.app()["processor_use"] == "gentle"
    page.select_option("#run-processor", "balanced")
    until(page, "document.getElementById('res-row').textContent.includes('Using 4 of 8 cores')")


def test_w20_connection_lost(w: World) -> None:
    page = w.page
    before = len(w.errors)
    page.route("**/progress", lambda r: r.abort())
    page.wait_for_selector("#lost-banner:not([hidden])", timeout=15_000)
    assert page.get_by_text("Lost connection to Baleen.").is_visible()
    shot(w, "W-20-lost", "lost")
    page.unroute("**/progress")
    page.wait_for_selector("#lost-banner[hidden]", state="attached", timeout=15_000)
    del w.errors[before:]  # htmx logs the polls this test aborted on purpose


def test_w08_completed(w: World) -> None:
    page = w.page
    fake_routes.GATE.set()
    wait_results(page)
    assert page.locator(".headline").inner_text() == "8 of 1,670 files need your attention"
    assert page.locator("#run-pill").inner_text() == "Completed · 8 to look at"
    tiles = page.locator("#tiles .tile")
    assert [t.split("\n")[0] for t in tiles.all_inner_texts()] == [
        "Failed", "Needs review", "Unsupported", "OK", "Ignored", "Skipped"]
    assert page.locator("#results-table tbody tr").first.locator(".badge").inner_text() == "Failed"
    assert page.title() in ("✓ Done · Convert · Baleen", "Run · Baleen")
    page.locator("#scroll").evaluate("el => el.scrollTop = 0")
    shot(w, "W-08-run-done", "run-done")
    # Actions (UI-R3): Open output folder, Download report, more (copy run ID, reveal report).
    page.click("#run-open-output")
    page.wait_for_selector("#toast.show")
    assert page.locator("#toast").inner_text().startswith("Opened in ")
    assert harness.REVEALED[-1] == str(w.out)
    page.click("#run-more")
    assert page.locator("#run-menu").is_visible()
    assert page.evaluate("document.activeElement.id") == "menu-copy-id"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "menu-reveal-report"
    page.keyboard.press("Escape")
    assert not page.locator("#run-menu").is_visible()
    assert page.evaluate("document.activeElement.id") == "run-more"


def test_w08b_filtered(w: World) -> None:
    page = w.page
    page.click("#tile-review")
    until(page, "document.getElementById('tile-review').getAttribute('aria-pressed') === 'true'")
    settle(page, 120)
    rows = page.locator("#results-table tbody tr")
    assert rows.count() == 5
    assert "status=review" in page.url
    shot(w, "W-08b-run-filtered", "run-filtered")


def test_w09_inspector(w: World) -> None:
    page = w.page
    row = page.locator("#results-table tbody tr", has_text="notes.txt")
    row.click()
    page.wait_for_selector("#inspector.open #insp-head")
    settle(page)
    insp = page.locator("#inspector")
    assert "Text encoding not certain" in insp.inner_text()
    assert "ENCODING_UNCERTAIN" in insp.inner_text()
    assert page.locator("#main.docked").count() == 1
    assert not page.locator("#results-table th.col-output").is_visible()  # docked at >= 1440 px (D-04)
    shot(w, "W-09-run-inspector", "run-inspector",
         proto_steps=lambda p: None)
    # ↑↓ move the selection and the inspector follows; Esc closes and returns focus to the row.
    # (notes.txt is the last "Needs review" row in plan order, so go up.)
    n = page.locator("#insp-head").get_attribute("data-n")
    page.locator(f"#row-{n}").focus()
    page.keyboard.press("ArrowUp")
    until(page, f"document.getElementById('insp-head') && document.getElementById('insp-head').dataset.n !== '{n}'")
    n2 = page.locator("#insp-head").get_attribute("data-n")
    assert page.locator(f"#row-{n2}").get_attribute("aria-selected") == "true"
    page.keyboard.press("Escape")
    until(page, "!document.getElementById('inspector').classList.contains('open')")
    assert page.evaluate("document.activeElement.id") == f"row-{n2}"


def test_w09b_inspector_ok_video(w: World) -> None:
    page = w.page
    page.click("#tile-review")  # clear the filter
    until(page, "document.getElementById('tile-review').getAttribute('aria-pressed') === 'false'")
    ready(page, "#rows-q")
    page.fill("#rows-q", "DSCF0128")
    until(page, "document.querySelectorAll('#results-table tbody tr[data-n]').length === 1")
    page.locator("#results-table tbody tr[data-n]").first.click()
    page.wait_for_selector("#inspector.open #insp-head")
    settle(page)
    text = page.locator("#inspector").inner_text()
    assert "V-AV-DUR" in text and "58.04 s vs 58.00 s" in text and "V-HASH" in text
    shot(w, "W-09b-run-inspector-ok", "run-inspector-ok")
    page.keyboard.press("Escape")


# --------------------------------------------------------------------------- W-12b, W-10, W-11


def test_w12b_check_results(w: World) -> None:
    go(w, "/check")
    w.page.click("#ab-start")
    w.page.wait_for_url(re.compile(r"/runs/"))
    wait_results(w.page)
    assert w.page.locator(".headline").inner_text() == "3 of 7 files need your attention"
    assert w.page.locator("#results-table th", has_text="Output").count() == 0
    shot(w, "W-12b-check-done", "check-done")


def test_w10_cancelled(w: World) -> None:
    page = w.page
    batch = w.tmp / "Users" / "mia" / "Desktop" / "test batch"
    harness.make_batch(batch)
    fake_routes.GATE.clear()
    go(w, "/convert")
    fill_folder(page, "source_dir", str(batch))
    fill_folder(page, "output_dir", str(w.tmp / "Users" / "mia" / "Baleen output" / "test batch"))
    page.click("#ab-start")
    page.wait_for_selector("#overlay.open #confirm-start")
    page.click("#confirm-start")
    page.wait_for_url(re.compile(r"/runs/"))
    w.h.wait_held(1)
    page.wait_for_selector("#run-cancel")
    page.click("#run-cancel")
    page.wait_for_selector("#overlay.open #cancel-keep")
    settle(page)
    assert page.evaluate("document.activeElement.id") == "cancel-keep"  # D-17
    shot(w, "cancel-dialog", None)
    page.click("#cancel-confirm")
    page.wait_for_selector("text=/Stopping after \\d+ files? in progress/", timeout=10_000)
    fake_routes.GATE.set()
    wait_results(page)
    assert page.locator("#run-pill").inner_text() == "Cancelled"
    assert re.match(r"Run cancelled after \d+ of 24 files", page.locator(".notice.warn .n-title").inner_text())
    assert page.locator("#tile-skipped .t-num").inner_text() != "0"
    shot(w, "W-10-run-cancelled", "run-cancelled")


def test_w11_stopped(w: World) -> None:
    page = w.page
    src = w.tmp / "Volumes" / "Archive2" / "04 Events (2000–2005)"
    order = harness.make_archive(src)
    harness.hold_files(src, order, 812, 4)
    out = w.tmp / "Volumes" / "Backup" / "04 Events"
    fake_routes.GATE.clear()
    go(w, "/convert")
    fill_folder(page, "source_dir", str(src))
    fill_folder(page, "output_dir", str(out))
    page.click("#ab-start")
    page.wait_for_selector("#overlay.open #confirm-start")
    page.click("#confirm-start")
    page.wait_for_url(re.compile(r"/runs/"))
    w.h.wait_held(4)
    fake_routes.Fault.out_root, fake_routes.Fault.on = str(out), True
    fake_routes.GATE.set()
    w.h.wait_idle()
    fake_routes.Fault.on = False
    wait_results(page)
    assert page.locator("#run-pill").inner_text() == "Stopped"
    assert page.locator(".notice.alert .n-title").inner_text() == "The run stopped: the output drive was disconnected"
    assert "data/logs/baleen.log" in page.locator(".notice.alert").inner_text()
    shot(w, "W-11-run-stopped", "run-stopped")


# --------------------------------------------------------------------------- W-13, W-22, collapsed, W-18, W-19


def test_w13_runs_history(w: World) -> None:
    go(w, "/runs")
    rows = w.page.locator("#runs-table tbody tr")
    assert rows.count() == 4
    text = w.page.locator("#runs-table").inner_text()
    assert "8 to look at" in text and "Cancelled" in text and "Stopped" in text
    assert w.page.locator("#rail-runs .count").inner_text() == "4"
    shot(w, "W-13-runs", "runs")
    w.page.click("#runs-wf-check")
    until(w.page, "document.querySelectorAll('#runs-table tbody tr').length === 1")
    w.page.click("#runs-wf-all")


def test_w22_narrow_drawer(w: World) -> None:
    page = w.page
    page.set_viewport_size(NARROW)
    go(w, "/convert")
    assert page.locator("#drawer-open").is_visible()
    page.click("#drawer-open")
    until(page, "document.getElementById('app').classList.contains('drawer')")
    settle(page)
    shot(w, "W-22-narrow-drawer", "convert-ready", viewport=NARROW,
         proto_steps=lambda p: p.click("[data-act=drawer-open]"))
    page.keyboard.press("Escape")
    until(page, "!document.getElementById('app').classList.contains('drawer')")
    assert page.evaluate("document.activeElement.id") == "drawer-open"
    page.set_viewport_size(VIEW)


def test_rail_collapsed(w: World) -> None:
    page = w.page
    go(w, "/convert")
    page.click("#rail-collapse")
    until(page, "document.getElementById('app').classList.contains('collapsed')")
    assert page.locator("#rail-collapse").get_attribute("aria-label") == "Expand sidebar"
    page.reload()
    assert "collapsed" in (page.locator("#app").get_attribute("class") or "")  # remembered (localStorage)
    settle(page)
    shot(w, "rail-collapsed", "collapsed")
    page.click("#rail-collapse")


def test_w18_quit_dialog(w: World) -> None:
    page = w.page
    go(w, "/convert")
    page.click("#rail-quit")
    page.wait_for_selector("#overlay.open #quit-stay")
    settle(page)
    assert page.evaluate("document.activeElement.id") == "quit-stay"  # D-17
    assert page.get_by_text("Baleen stops and this tab can be closed.").is_visible()
    shot(w, "W-18-quit", "quit")
    page.keyboard.press("Escape")
    until(page, "!document.getElementById('overlay').classList.contains('open')")
    assert page.evaluate("document.activeElement.id") == "rail-quit"


def test_w19_stopped_after_quit(w: World) -> None:
    page = w.page
    page.click("#rail-quit")
    page.wait_for_selector("#overlay.open #quit-confirm")
    page.click("#quit-confirm")
    page.wait_for_url(re.compile(r"/stopped$"), timeout=20_000)
    assert page.locator("h1").inner_text() == "Baleen has stopped"
    shot(w, "W-19-stopped", "stopped")
    w.h.thread.join(timeout=15)
    assert not w.h.thread.is_alive()


def test_no_page_errors(w: World) -> None:
    """No CSP violations or script errors on the way (SEC-6: no inline scripts or styles)."""
    bad = [e for e in w.errors if "Failed to load resource" not in e and "ERR_FAILED" not in e]
    assert not bad, bad

