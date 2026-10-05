"""Settings › Resource use (UI-S2, D-21, D-22): the "With these settings" fragment from
GET /settings/plan, computed by the server with the §5.5 rules. The expected numbers come from
the design prototype's resolve() (the test oracle named in design §12)."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from tests.ui import harness

PORT = 8776


def oracle(cores: int, ram: int, network: bool, r: dict) -> dict:
    """The prototype's resolve(), transcribed line by line."""
    c = cores
    p = r.get("processor_use", "balanced")
    if p == "gentle":
        b = max(1, c // 4)
    elif p == "maximum":
        b = max(1, c - 1)
    elif p == "custom":
        b = min(c, max(1, r["processor_cores"]))
    else:
        b = max(2, c // 2)
    b = min(b, c)
    m = min(ram - 2, max(1, r["memory_gb"])) if r.get("memory_limit") == "custom" else \
        max(1, min(int(ram * 0.25 + 0.5), ram - 4))
    t = min(16, max(1, r["transfer_count"])) if r.get("transfer_slots") == "custom" else (4 if network else 8)
    k = min(4, max(1, b // 2), max(1, m // 1))
    f = 2 if b >= 8 else 1
    return {"B": b, "M": m, "T": t, "K": k, "F": f, "threads": max(1, b // f)}


def numbers(fragment: str) -> dict:
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return {
        "B": int(re.search(r"(\d+) file tasks at once", text).group(1)),
        "K": int(re.search(r"(\d+) document converters?", text).group(1)),
        "F": int(re.search(r"(\d+) video encoders?", text).group(1)),
        "threads": int(re.search(r"using (\d+) cores?", text).group(1)),
        "T": int(re.search(r"(\d+) file transfers", text).group(1)),
        "M": int(re.search(r"Up to (\d+) GB", text).group(1)),
        "cores": int(re.search(r"never use more than \d+ of (\d+) cores", text).group(1)),
    }


@pytest.mark.parametrize(("cores", "ram", "network"), [(8, 16, True), (28, 80, False), (4, 8, False), (2, 4, True)])
@pytest.mark.parametrize("prefs", [
    {},
    {"processor_use": "gentle"},
    {"processor_use": "maximum"},
    {"processor_use": "custom", "processor_cores": 6},
    {"memory_limit": "custom", "memory_gb": 6},
    {"transfer_slots": "custom", "transfer_count": 2},
    {"processor_use": "custom", "processor_cores": 1, "memory_limit": "custom", "memory_gb": 1},
])
def test_plan_matches_the_prototype_oracle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cores: int, ram: int,
                                           network: bool, prefs: dict) -> None:
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch, cores=cores, ram=ram, network=network)
    base = {"processor_cores": 4, "memory_gb": 4, "transfer_count": 4}
    want = {**base, **prefs}
    if want.get("memory_limit") == "custom":
        want["memory_gb"] = min(max(1, ram - 2), want["memory_gb"])
    if prefs:
        ctx.store.update(prefs)
    got = numbers(harness.client(ctx).get("/settings/plan").text)
    exp = oracle(cores, ram, network, want)
    assert {k: got[k] for k in exp} == exp
    assert got["cores"] == cores


def test_plan_speaks_plain_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    raw = harness.client(ctx).get("/settings/plan").text
    frag = html.unescape(re.sub(r"<[^>]+>", "", raw))
    assert "With these settings, Baleen runs at most" in frag
    assert "4 file transfers to and from network folders" in frag
    assert "1 video encoder, using 4 cores" in frag
    assert 'aria-live="polite"' in raw
    for jargon in ("token", "thread", "worker"):
        assert jargon not in frag.lower()


def test_plan_previews_unsaved_values_without_saving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """While the memory slider moves, the summary follows (GET, nothing saved)."""
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    c = harness.client(ctx)
    frag = c.get("/settings/plan", params={"memory_limit": "custom", "memory_gb": "6"}).text
    assert numbers(frag)["M"] == 6
    assert ctx.store.app()["memory_limit"] == "auto"
    bad = c.get("/settings/plan", params={"memory_gb": "lots", "processor_use": "warp", "work_dir": "/x"}).text
    assert numbers(bad)["B"] == 4  # invalid or non-resource values are ignored


def test_resource_controls_save_and_switching_to_custom_starts_from_the_current_value(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    c = harness.client(ctx)
    r = c.post("/api/settings", data={"ui": "resource", "processor_use": "custom"})
    assert ctx.store.app()["processor_cores"] == 4  # Balanced on 8 cores
    assert "Uses 4 of 8 cores" in r.text and 'id="res-processor_cores-up"' in r.text
    r = c.post("/api/settings", data={"ui": "resource", "processor_cores": "99"})
    assert ctx.store.app()["processor_cores"] == 8  # clamped to the cores
    r = c.post("/api/settings", data={"ui": "resource", "memory_limit": "custom"})
    assert ctx.store.app()["memory_gb"] == 4 and 'id="res-memory"' in r.text and 'max="14"' in r.text
    r = c.post("/api/settings", data={"ui": "resource", "transfer_slots": "custom"})
    assert ctx.store.app()["transfer_count"] == 4
    r = c.post("/api/settings", data={"ui": "resource", "reset": "resources"})
    app = ctx.store.app()
    assert (app["processor_use"], app["memory_limit"], app["transfer_slots"]) == ("balanced", "auto", "auto")
    assert "Resource use reset to defaults" in r.headers["hx-trigger"]
    assert re.search(r'id="res-defaults"\s+disabled', r.text)


def test_engine_reads_changes_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changes save immediately (§11): the settings file is written at once."""
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    harness.client(ctx).post("/api/settings", data={"ui": "resource", "low_priority": "false"})
    assert '"low_priority": false' in ctx.home.settings_path.read_text(encoding="utf-8")
