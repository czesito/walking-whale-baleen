"""AC-14: the Convert and Check flows completed with the keyboard only (Chromium here; Safari,
Firefox and Edge are on the manual checklist). No mouse event is sent in these tests."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from . import harness

pytestmark = pytest.mark.ui

PORT = 8786


@pytest.fixture(scope="module")
def kb(browser, mpm, tmp_path_factory):  # noqa: ANN001, ANN201
    tmp = tmp_path_factory.mktemp("keyboard")
    h = harness.start(tmp, PORT, mpm)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(h.auth_url)
    yield h, page, tmp
    ctx.close()
    h.stop()


def active(page: Any) -> str:
    return page.evaluate("document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : ''")


def until(page: Any, js: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not page.evaluate(js):
        if time.monotonic() > deadline:
            raise TimeoutError(js)
        time.sleep(0.05)


def tab_to(page: Any, target: str, *, back: bool = False, limit: int = 160) -> None:
    """Press Tab (or Shift+Tab) until the element with id `target` has focus."""
    for _ in range(limit):
        if active(page) == target:
            return
        page.keyboard.press("Shift+Tab" if back else "Tab")
    raise AssertionError(f"Tab never reached #{target} (stuck on {active(page)})")


def type_folder(page: Any, key: str, value: str) -> None:
    tab_to(page, f"f-{key}")
    page.evaluate(f"document.getElementById('f-{key}-msg').dataset.stale = '1'")
    page.keyboard.press("Control+A")
    page.keyboard.type(value)
    page.keyboard.press("Tab")  # blur → validation
    until(page, f"document.getElementById('f-{key}-msg').dataset.stale === undefined")


def test_convert_flow_keyboard_only(kb) -> None:  # noqa: ANN001
    h, page, tmp = kb
    src = tmp / "archive"
    for rel, data in harness.PROTOTYPE_ROWS:
        harness.write(src, rel, data)
    page.goto(h.base + "/convert")
    type_folder(page, "source_dir", str(src))
    assert page.locator("#f-source_dir-msg").inner_text() == "Readable · read-only for Baleen"
    type_folder(page, "output_dir", str(tmp / "out"))
    assert page.locator("#f-output_dir-msg").inner_text().startswith("Writable")
    # Segmented control: roving tabindex, ← → move the selection (design §09).
    tab_to(page, "opt-pdfa_level-2b")
    page.keyboard.press("ArrowLeft")
    until(page, "document.getElementById('opt-pdfa_level-1b').getAttribute('aria-pressed') === 'true'")
    assert active(page) == "opt-pdfa_level-1b"  # focus kept across the swap
    assert h.ctx.store.workflow()["pdfa_level"] == "1b"
    page.keyboard.press("ArrowRight")
    until(page, "document.getElementById('opt-pdfa_level-2b').getAttribute('aria-pressed') === 'true'")
    # Switch with Space.
    tab_to(page, "opt-hide_email_addresses")
    page.keyboard.press("Space")
    until(page, "document.getElementById('opt-hide_email_addresses').getAttribute('aria-checked') === 'true'")
    page.keyboard.press("Space")
    until(page, "document.getElementById('opt-hide_email_addresses').getAttribute('aria-checked') === 'false'")
    # Preview, then Start → dialog (focus on "Start converting") → Enter.
    tab_to(page, "prv-run")
    page.keyboard.press("Enter")
    until(page, "document.querySelector('#preview-card[data-preview=done]') !== null")
    page.wait_for_timeout(300)  # settle
    assert active(page) in ("prv-refresh", "preview-card")  # not lost to <body>
    tab_to(page, "ab-start")
    page.keyboard.press("Enter")
    until(page, "document.getElementById('overlay').classList.contains('open')")
    until(page, "document.activeElement && document.activeElement.id === 'confirm-start'")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert active(page) == "confirm-start"  # focus is trapped in the dialog
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/runs/\d{8}-\d{6}"))
    page.wait_for_selector("#run-results", timeout=60_000)
    assert page.locator(".headline").inner_text() == "8 of 25 files need your attention"
    # Results: a tile filters (Enter), rows open the inspector (Enter), ↑↓ follow, Esc returns focus.
    tab_to(page, "tile-review")
    page.keyboard.press("Enter")
    until(page, "document.getElementById('tile-review').getAttribute('aria-pressed') === 'true'")
    assert active(page) == "tile-review"
    first_row = page.locator("#results-table tbody tr[data-n]").first.get_attribute("id")
    tab_to(page, first_row)
    page.keyboard.press("Enter")
    until(page, "document.getElementById('inspector').classList.contains('open')")
    assert active(page) == first_row
    page.keyboard.press("ArrowDown")
    second = page.locator("#results-table tbody tr[data-n]").nth(1).get_attribute("data-n")
    head = "document.getElementById('insp-head')"
    until(page, f"{head} && {head}.dataset.n === '{second}'")
    page.keyboard.press("Escape")
    until(page, "!document.getElementById('inspector').classList.contains('open')")
    assert active(page) == f"row-{second}"
    # "/" focuses the path filter (design §09).
    page.keyboard.press("/")
    assert active(page) == "rows-q"


def test_check_flow_keyboard_only(kb) -> None:  # noqa: ANN001
    h, page, tmp = kb
    folder = tmp / "publications"
    harness.make_check_tree(folder)
    page.goto(h.base + "/check")
    type_folder(page, "check_dir", str(folder))
    assert page.locator("#actionbar .status").inner_text() == "Ready to check. Nothing will be written to this folder."
    tab_to(page, "ab-start")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/runs/"))
    page.wait_for_selector("#run-results", timeout=60_000)
    assert page.locator(".headline").inner_text() == "3 of 7 files need your attention"
    row = page.locator("#results-table tbody tr[data-n]").first.get_attribute("id")
    tab_to(page, row)
    page.keyboard.press("Enter")
    until(page, "document.getElementById('inspector').classList.contains('open')")
    until(page, "document.getElementById('inspector').textContent.includes('Not archival yet')")
    page.keyboard.press("Escape")


def test_quit_dialog_and_rail_by_keyboard(kb) -> None:  # noqa: ANN001
    h, page, _ = kb
    page.goto(h.base + "/runs")
    tab_to(page, "rail-quit")
    page.keyboard.press("Enter")
    until(page, "document.activeElement && document.activeElement.id === 'quit-stay'")  # D-17
    page.keyboard.press("Enter")  # Stay
    until(page, "!document.getElementById('overlay').classList.contains('open')")
    assert active(page) == "rail-quit"
    # Runs table rows open with Enter.
    tab_to(page, page.locator("#runs-table tbody tr").first.get_attribute("id"))
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/runs/\d{8}-\d{6}"))
