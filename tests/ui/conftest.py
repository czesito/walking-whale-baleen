"""Browser tests (marker `ui`): Chromium via Playwright against a real in-process server.

Screenshots of every W-state, next to the design prototype's same state, are written to
$BALEEN_UI_SHOTS (app-*.png, proto-*.png) when that variable is set.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "docs" / "prototype" / "baleen-prototype.html"
FONTS = ROOT / "src" / "baleen" / "static" / "fonts"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).resolve().parent
    for item in items:
        if Path(str(item.fspath)).resolve().parent == here:
            item.add_marker(pytest.mark.ui)


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser not installed
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def mpm() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def route_prototype_fonts(page: Any) -> None:
    """The prototype links Google Fonts; serve the bundled subsets instead (offline, same faces)."""
    faces = (("Inter", "Inter-400.woff2", 400), ("Inter", "Inter-500.woff2", 500), ("Inter", "Inter-600.woff2", 600),
             ("IBM Plex Mono", "IBMPlexMono-400.woff2", 400), ("IBM Plex Mono", "IBMPlexMono-500.woff2", 500),
             ("Cormorant Garamond", "CormorantGaramond-600.woff2", 600))
    css = "".join(f'@font-face{{font-family:"{fam}";src:url("https://fonts.gstatic.com/baleen/{f}") '
                  f'format("woff2");font-weight:{w};font-display:block}}' for fam, f, w in faces)
    page.route("https://fonts.googleapis.com/**",
               lambda r: r.fulfill(status=200, content_type="text/css", body=css))
    page.route("https://fonts.gstatic.com/baleen/**",
               lambda r: r.fulfill(status=200, content_type="font/woff2",
                                   body=(FONTS / r.request.url.rsplit("/", 1)[1]).read_bytes()))
