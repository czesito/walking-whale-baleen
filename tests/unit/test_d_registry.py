"""Workflow registry (spec §12.8, design D-02, D-05): adding a workflow needs no template change.

A dummy workflow registered at run time gets its rail item, setup page, options panel,
confirmation, start endpoint and run page from the shared templates alone."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from baleen import workflows
from baleen.model import Mode
from baleen.workflows import Explainer, FolderField, OptionField, OptionGroup, Workflow
from tests.ui import harness

PORT = 8775

DUMMY = Workflow(
    id="dummy",
    name="Dummy",
    icon="layers",
    order=30,
    description="A test workflow that checks a folder.",
    kicker="Workflow",
    mode=Mode.CHECK,
    folders=(FolderField("check_dir", "Folder to inspect", "read", "Only read."),),
    options=(OptionGroup("Testing", (
        OptionField("video_quality", "Quality knob", "Picks a quality.", "seg", (("high", "High"), ("standard", "Std")),
                    "high"),
        OptionField("hide_email_addresses", "Hide things", "Hides things.", "switch", (), False),
    )),),
    preview=False,
    start_label="Start inspecting",
    confirm=True,
    result_columns=("status", "source", "action", "reason"),
    explainer=Explainer("What the dummy does", ("Everything · nothing at all",), icons=("shield",)),
    ready_text="Ready to inspect.",
)


@pytest.fixture()
def dummy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    workflows.all_workflows()  # load the built-in ones first
    monkeypatch.setitem(workflows._REGISTRY, "dummy", DUMMY)
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    yield ctx, harness.client(ctx), tmp_path
    ctx.engine.wait(30)


def text(r) -> str:  # noqa: ANN001
    return html.unescape(r.text)


def test_rail_lists_every_registered_workflow_in_order(dummy) -> None:  # noqa: ANN001
    _ctx, c, _ = dummy
    page = text(c.get("/runs", headers={"hx-request": ""}))
    rail = page.split('class="rail-label">Workflows</div>')[1].split("</div>")[0]
    assert rail.index(">Convert<") < rail.index(">Check<") < rail.index(">Dummy<")
    assert 'href="/dummy"' in rail and "#i-layers" in rail


def test_setup_page_comes_from_the_registration(dummy) -> None:  # noqa: ANN001
    _ctx, c, _ = dummy
    page = text(c.get("/dummy", headers={"hx-request": ""}))
    for t in ("A test workflow that checks a folder.", "Folder to inspect", "Only read.", "Quality knob",
              "Hide things", "What the dummy does", "<b>Everything</b> · nothing at all", "Start inspecting",
              "Choose the folders above.", 'id="opt-video_quality-high" aria-pressed="true"', "<title>Dummy · Baleen"):
        assert t in page, t
    assert 'aria-current="page"' in page.split('href="/dummy"')[1][:80] or 'href="/dummy" data-tip="Dummy" ' \
        'id="rail-dummy" aria-current="page"' in page


def test_options_confirmation_and_run_page_for_a_new_workflow(dummy) -> None:  # noqa: ANN001
    ctx, c, tmp = dummy
    r = c.post("/api/settings", data={"ui": "options:dummy", "video_quality": "standard"})
    assert 'id="opt-video_quality-standard" aria-pressed="true"' in r.text and "Default (modified)" in r.text
    folder = tmp / "things"
    harness.make_check_tree(folder)
    r = c.post("/api/dummy", data={"check_dir": str(folder)})
    assert r.status_code == 200 and "Start inspecting?" in r.text
    assert json.loads(r.headers["hx-trigger-after-settle"]) == {"baleen:modal-open": True}
    assert "Quality knob: Std" in r.text and "Hide things: off" in r.text  # chips from the schema
    r = c.post("/api/dummy", data={"check_dir": str(folder), "confirmed": "1"})
    assert r.status_code == 204
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    assert ctx.engine.wait(30)
    page = text(c.get(f"/runs/{run_id}", headers={"hx-request": ""}))
    assert "3 of 7 files need your attention" in page
    assert ">Output<" not in page  # result_columns without output


def test_unknown_workflow_ids_are_404(dummy) -> None:  # noqa: ANN001
    _ctx, c, _ = dummy
    assert c.get("/nosuch", headers={"hx-request": ""}).status_code == 404
    assert c.post("/api/nosuch").status_code == 404
