"""Behaviour in the browser: the Browse waiting state (UI-C6), tab title and the opt-in finish
notification (UI-G2, D-16), the inspector docking at 1440 px (D-04), the connection banner after
three failed polls (UI-G3), and the rail job card on other pages."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from . import fake_routes, harness

pytestmark = pytest.mark.ui

PORT = 8787

RECORD_NOTIFICATIONS = """
window.__notes = [];
class FakeNotification {
  constructor(title, opts) { window.__notes.push(title); this.title = title; window.__lastNote = this; }
  close() {}
  static get permission() { return 'granted'; }
  static requestPermission() { return Promise.resolve('granted'); }
}
window.Notification = FakeNotification;
"""


@pytest.fixture(scope="module")
def bh(browser, mpm, tmp_path_factory):  # noqa: ANN001, ANN201
    tmp = tmp_path_factory.mktemp("behaviour")
    h = harness.start(tmp, PORT, mpm)
    yield h, browser, tmp
    fake_routes.GATE.set()
    h.stop()


def new_page(browser: Any, h: harness.Harness, width: int = 1440, height: int = 900) -> tuple[Any, Any]:
    ctx = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
    ctx.add_init_script(RECORD_NOTIFICATIONS)
    page = ctx.new_page()
    page.goto(h.auth_url)
    return ctx, page


def until(page: Any, js: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not page.evaluate(js):
        if time.monotonic() > deadline:
            raise TimeoutError(js)
        time.sleep(0.05)
    page.wait_for_timeout(60)  # let htmx settle (attach listeners to) what it just swapped in


def test_browse_waiting_state(bh) -> None:  # noqa: ANN001
    """UI-C6: while the OS dialog is open the button says so, with the hint; then the path fills in."""
    h, browser, tmp = bh
    picked = tmp / "picked folder 訪談"
    picked.mkdir()
    harness.FakePicker.path, harness.FakePicker.delay = str(picked), 1.5
    ctx, page = new_page(browser, h)
    page.goto(h.base + "/check")
    page.focus("#f-check_dir-browse")
    page.keyboard.press("Enter")
    until(page, "document.getElementById('f-check_dir-field').classList.contains('htmx-request')")
    assert page.get_by_text("Waiting for the folder dialog…").is_visible()
    assert page.get_by_text("Don't see it? It may be behind this window.").is_visible()
    until(page, "document.getElementById('f-check_dir').value !== ''")
    until(page, "!document.getElementById('f-check_dir-field').classList.contains('htmx-request')")
    assert page.locator("#f-check_dir").input_value() == str(picked)
    assert page.locator("#f-check_dir-msg").inner_text() == "Readable"
    assert not page.get_by_text("Waiting for the folder dialog…").is_visible()
    assert page.evaluate("document.activeElement.id") == "f-check_dir-browse"
    # Cancelling the dialog changes nothing.
    harness.FakePicker.path, harness.FakePicker.delay = None, 0.2
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    assert page.locator("#f-check_dir").input_value() == str(picked)
    ctx.close()


def test_tab_title_rail_card_and_finish_notification(bh) -> None:  # noqa: ANN001
    h, browser, tmp = bh
    src = tmp / "batch"
    harness.make_jpegs(src, n=24, hold_from=10)
    h.ctx.store.update({"notify_on_finish": True, "check_dir": str(src)})
    ctx, page = new_page(browser, h)
    fake_routes.GATE.clear()
    page.goto(h.base + "/check")
    page.click("#ab-start")
    page.wait_for_url(re.compile(r"/runs/"))
    h.wait_held(1)
    page.goto(h.base + "/settings")  # another page: the rail card polls /job every 2 s
    until(page, "document.querySelector('#railjob .rail-job') !== null")
    assert page.locator("#railjob .txt").inner_text() == "Checking"
    assert re.match(r"\d+% · Check · Baleen$", page.title())
    assert page.locator("body").get_attribute("data-notify") == "true"
    page.click("#rail-quit")  # W-18 while a run is going: it says the run finishes first
    until(page, "document.activeElement && document.activeElement.id === 'quit-stay'")
    assert page.get_by_text("A run is in progress. Baleen finishes the files in progress, saves the report, "
                            "then quits.").is_visible()
    page.keyboard.press("Escape")
    fake_routes.GATE.set()
    until(page, "document.title === '✓ Done · Check · Baleen'", timeout=30)
    assert page.evaluate("window.__notes") == ["Check finished · 24 files · all OK"]
    page.evaluate("window.__lastNote.onclick()")  # clicking it opens the run
    page.wait_for_url(re.compile(r"/runs/\d{8}-\d{6}$"))
    ctx.close()


def test_progress_title_can_be_turned_off(bh) -> None:  # noqa: ANN001
    h, browser, tmp = bh
    ctx, page = new_page(browser, h)
    page.goto(h.base + "/settings")
    page.click("#pref-title")
    until(page, "document.getElementById('pref-title').getAttribute('aria-checked') === 'false'")
    assert page.locator("body").get_attribute("data-progress-title") == "false"
    assert h.ctx.store.app()["progress_in_title"] is False
    page.click("#pref-title")
    until(page, "document.getElementById('pref-title').getAttribute('aria-checked') === 'true'")
    assert page.locator("body").get_attribute("data-progress-title") == "true"
    ctx.close()


def test_inspector_docks_at_1440_and_overlays_below(bh) -> None:  # noqa: ANN001
    h, browser, tmp = bh
    src = tmp / "dock"
    for rel, data in harness.PROTOTYPE_ROWS:
        harness.write(src, rel, data)
    h.ctx.store.update({"check_dir": str(src)})
    ctx, page = new_page(browser, h, 1440)
    page.goto(h.base + "/check")
    page.click("#ab-start")
    page.wait_for_url(re.compile(r"/runs/"))
    page.wait_for_selector("#run-results", timeout=30_000)
    url = page.url
    page.locator("#results-table tbody tr[data-n]").first.click()
    until(page, "document.getElementById('inspector').classList.contains('open')")
    until(page, "getComputedStyle(document.getElementById('scroll')).marginRight === '420px'", timeout=5)
    ctx.close()
    ctx, page = new_page(browser, h, 1280)
    page.goto(url)
    page.locator("#results-table tbody tr[data-n]").first.click()
    until(page, "document.getElementById('inspector').classList.contains('open')")
    assert page.evaluate("getComputedStyle(document.getElementById('scroll')).marginRight") == "0px"  # overlay
    assert page.locator("#results-table th.col-action").is_visible()  # columns stay below 1440 px
    ctx.close()
    ctx, page = new_page(browser, h, 900, 700)
    page.goto(url)
    page.locator("#results-table tbody tr[data-n]").first.click()
    until(page, "document.getElementById('inspector').classList.contains('open')")
    assert page.evaluate("document.getElementById('inspector').getBoundingClientRect().width") == 900  # sheet
    ctx.close()


def test_banner_needs_three_failed_polls_and_clears_itself(bh) -> None:  # noqa: ANN001
    h, browser, tmp = bh
    src = tmp / "lost"
    harness.make_jpegs(src, n=12, hold_from=1)
    h.ctx.store.update({"check_dir": str(src)})
    ctx, page = new_page(browser, h)
    fake_routes.GATE.clear()
    page.goto(h.base + "/check")
    page.click("#ab-start")
    page.wait_for_url(re.compile(r"/runs/"))
    aborted: list[str] = []

    def abort(route: Any) -> None:
        aborted.append(route.request.url)
        route.abort()

    page.route("**/progress", abort)
    until(page, "!document.getElementById('lost-banner').hidden", timeout=20)
    assert len(aborted) >= 3
    first_seen = len(aborted)
    page.unroute("**/progress")
    until(page, "document.getElementById('lost-banner').hidden", timeout=20)  # retries every 5 s
    assert first_seen <= 4
    fake_routes.GATE.set()
    page.wait_for_selector("#run-results", timeout=30_000)
    ctx.close()
