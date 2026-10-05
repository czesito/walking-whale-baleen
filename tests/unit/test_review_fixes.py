"""Regression tests for the independent review's findings (cancel while planning, links in the
output root, recovery of live runs, unreadable folders, folders at output paths, publish intents)."""

from __future__ import annotations

import copy
import getpass
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baleen.convert import base
from baleen.fsops import OutputLock
from baleen.home import Home
from baleen.journal import Journal
from baleen.model import Action, Category, ItemResult, Mode, PlanItem
from baleen.report import read_csv
from baleen.runner import Engine, JobState, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

from . import fakes


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    fakes.install(monkeypatch)
    home = Home(tmp_path / "home")
    home.ensure()
    store = SettingsStore(home.settings_path)
    store.update({"processor_use": "custom", "processor_cores": 2, "keep_awake": False})
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    return Engine(home, store, Toolset(home)), store, src, out


def run(engine: Engine, store: SettingsStore, src: Path, out: Path):  # noqa: ANN201
    return engine.start(RunSpec(Mode.CONVERT, str(src), str(out), copy.deepcopy(store.snapshot())),
                        background=False)


def tree(root: Path) -> dict[str, str]:
    res = {}
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            p = Path(dp) / f
            res[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return res


def test_cancel_while_planning_runs_nothing(env, monkeypatch) -> None:  # noqa: ANN001
    engine, store, src, out = env

    class SlowProbe(fakes.FakeImage):
        def probe(self, ctx, src_ref):  # noqa: ANN001, ANN202
            time.sleep(0.05)
            return super().probe(ctx, src_ref)

    monkeypatch.setattr(base, "_registry", {"image": SlowProbe(), "document": fakes.FakeDocument()})
    for i in range(200):
        fakes.write(str(src), f"p{i:03}.bmp", b"bmp %d" % i)
    st = copy.deepcopy(store.snapshot())
    job = engine.start(RunSpec(Mode.CONVERT, str(src), str(out), st), background=True, prefs=lambda: st["app"])
    deadline = time.time() + 20
    while job.prepare_note != "Planning…" and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert engine.cancel()
    assert job.done_event.wait(60)
    assert job.state == JobState.CANCELLED
    rows = read_csv(job.report_path())
    assert len(rows) == 200
    assert {r.status for r in rows} == {"SKIPPED"} and {r.reason for r in rows} == {"CANCELLED"}
    assert not [p for p in tree(out) if not p.startswith("_baleen")], "nothing may be written"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_publish_never_follows_a_junction_into_the_source(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(str(src), "sub/b.bmp", b"bmp")
    out.mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(out / "sub"), str(src / "sub")], capture_output=True)
    if r.returncode != 0:
        pytest.skip("could not create a junction")
    before = tree(src)
    job = run(engine, store, src, out)
    assert tree(src) == before, "P1: something was written into the source through the junction"
    row = {x.source_path: x for x in read_csv(job.report_path())}["sub/b.bmp"]
    assert row.status == "NEEDS_REVIEW" and row.reason == "OUTPUT_OCCUPIED" and row.output_path == ""
    os.rmdir(out / "sub")  # remove the junction itself, not its target


def test_folder_at_the_output_path_is_occupied(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(str(src), "a.jpg", b"jpeg")
    (out / "a.jpg").mkdir(parents=True)
    job = run(engine, store, src, out)
    row = read_csv(job.report_path())[0]
    assert row.status == "NEEDS_REVIEW" and row.reason == "OUTPUT_OCCUPIED"
    assert (out / "a.jpg").is_dir()


def test_unreadable_subfolder_is_one_failed_row(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(str(src), "ok.jpg", b"jpeg")
    locked = src / "locked"
    fakes.write(str(src), "locked/x.jpg", b"jpeg")
    if sys.platform == "win32":
        user = os.environ.get("USERNAME") or getpass.getuser()
        # Deny listing only (RD): READ_CONTROL and WRITE_DAC stay, so the test can always undo it.
        r = subprocess.run(["icacls", str(locked), "/deny", f"{user}:(RD)"], capture_output=True)
        if r.returncode != 0:
            pytest.skip("could not deny access")
    else:
        os.chmod(locked, 0)
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that ignore permissions")
    try:
        job = run(engine, store, src, out)
        rows = {r.source_path: r for r in read_csv(job.report_path())}
        assert rows["ok.jpg"].status == "OK"
        assert rows["locked"].status == "FAILED" and rows["locked"].reason == "SOURCE_UNREADABLE"
    finally:
        if sys.platform == "win32":
            r = subprocess.run(["icacls", str(locked), "/remove:d", user], capture_output=True)
            assert r.returncode == 0, "could not remove the test's deny entry"
        else:
            os.chmod(locked, 0o755)


def _journal_without_report(tmp_path: Path, out: Path, run_id: str) -> Journal:
    rd = out / "_baleen"
    rd.mkdir(parents=True)
    it = PlanItem(n=1, source_path="a.jpg", abs_path=None, size=4, mtime_ns=None, ext=".jpg",
                  category=Category.IMAGE, action=Action.COPY, route="image", target_ext=".jpg", out_dir="",
                  output_path="a.jpg")
    return Journal.create(str(rd / f"journal-{run_id}.sqlite"),
                          {"run_id": run_id, "mode": "convert", "output_root": str(out)}, [it])


def test_recovery_leaves_a_live_run_alone(env, tmp_path: Path) -> None:  # noqa: ANN001
    engine, _store, _src, out = env
    j = _journal_without_report(tmp_path, out, "20260101-000000")
    j.close()
    lock = OutputLock(str(out / "_baleen" / ".run-20260101-000000.lock"))
    lock.acquire()  # held by this live process: another Baleen is running that run
    assert engine.recover_dir(str(out / "_baleen")) == []
    assert not (out / "_baleen" / "report-20260101-000000.csv").exists()
    lock.release()
    assert engine.recover_dir(str(out / "_baleen")) == ["20260101-000000"]


def test_recovery_reports_a_published_intent(env, tmp_path: Path) -> None:  # noqa: ANN001
    engine, _store, _src, out = env
    j = _journal_without_report(tmp_path, out, "20260101-000001")
    (out / "a.jpg").write_bytes(b"done")
    sha = hashlib.sha256(b"done").hexdigest()
    j.add_intent(ItemResult(n=1, run_id="20260101-000001", source_path="a.jpg", output_path="a.jpg",
                            output_size=4, output_sha256=sha, status="OK", action="copy"))
    j.close()
    assert engine.recover_dir(str(out / "_baleen")) == ["20260101-000001"]
    row = read_csv(str(out / "_baleen" / "report-20260101-000001.csv"))[0]
    assert row.status == "OK" and row.output_sha256 == sha  # not INTERRUPTED: the file was complete

