"""Engine behaviour with fake routes: P1, P2, P4, P5, P9, DR-09, resume, cancel, recovery."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import time
from pathlib import Path

import pytest

from baleen import settings as S
from baleen.fsops import OutputLock
from baleen.home import Home
from baleen.journal import Journal
from baleen.model import Mode
from baleen.report import read_csv
from baleen.runner import Engine, JobState, RootError, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

from . import fakes


def snapshot_tree(root: str) -> dict[str, tuple[int, int, str]]:
    """Listing + size + mtime + SHA-256 of every file (AC-01)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for d in dns:
            out[os.path.relpath(os.path.join(dp, d), root) + "/"] = (0, 0, "")
        for f in fns:
            p = os.path.join(dp, f)
            st = os.stat(p)
            digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, digest)
    return out


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    fakes.install(monkeypatch)
    home = Home(tmp_path / "home")
    home.ensure()
    store = SettingsStore(home.settings_path)
    store.update({"processor_use": "custom", "processor_cores": 4, "keep_awake": False})
    engine = Engine(home, store, Toolset(home))
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    return engine, store, str(src), str(out)


def make_tree(src: str) -> None:
    fakes.write(src, "a.jpg", b"jpeg-a")
    fakes.write(src, "b.bmp", b"bmp-b")
    fakes.write(src, "c.doc", b"doc-c")
    fakes.write(src, "d.doc", b"doc-d")
    fakes.write(src, "Thumbs.db", b"x")
    fakes.write(src, "x.zip", b"zip")
    fakes.write(src, "sub/e.jpg", b"jpeg-e")
    fakes.write(src, "sub/訪談 1.doc", b"doc-cjk")
    fakes.write(src, "photo.bmp", b"bmp-p")
    fakes.write(src, "photo.jpg", b"jpeg-p")


def run(engine: Engine, store: SettingsStore, mode: Mode, src: str, out: str | None, **overrides):  # noqa: ANN201
    st = copy.deepcopy(store.snapshot())
    for k, v in overrides.items():
        sec, _f = S.find_field(k)
        st[sec][k] = v
    job = engine.start(RunSpec(mode, src, out, st), background=False)
    assert job.done_event.is_set()
    return job


def rows_by_source(job) -> dict[str, dict]:  # noqa: ANN001
    return {r.source_path: r for r in read_csv(job.report_path())}


def test_convert_end_to_end(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    make_tree(src)
    before = snapshot_tree(src)
    job = run(engine, store, Mode.CONVERT, src, out)
    assert job.state == JobState.COMPLETED
    after = snapshot_tree(src)
    assert before == after, "P1: the source tree changed"

    rows = rows_by_source(job)
    # P9 / AC-02: every scanned file exactly once
    assert set(rows) == {k.replace("\\", "/") for k in before if not k.endswith("/")}
    assert rows["a.jpg"].status == "OK" and rows["a.jpg"].action == "copy"
    assert rows["b.bmp"].status == "OK" and rows["b.bmp"].action == "convert"
    assert rows["c.doc"].output_path == "c.pdf" and rows["c.doc"].status == "OK"
    assert "V-PDFA=pass(2b)" in rows["c.doc"].checks and "V-HASH=pass" in rows["c.doc"].checks
    assert rows["Thumbs.db"].status == "IGNORED" and rows["Thumbs.db"].reason == "SYSTEM_FILE"
    assert rows["x.zip"].status == "UNSUPPORTED" and rows["x.zip"].reason == "UNSUPPORTED_FORMAT"
    assert rows["x.zip"].source_sha256 == hashlib.sha256(b"zip").hexdigest()
    assert rows["photo.bmp"].output_path == "photo_bmp.jpg"
    assert rows["photo.jpg"].output_path == "photo_jpg.jpg"
    assert rows["sub/訪談 1.doc"].output_path == "sub/訪談 1.pdf"

    # AC-03: every output file has a row with matching SHA-256; nothing else exists
    outputs = {}
    for dp, dns, fns in os.walk(out):
        dns[:] = [d for d in dns if d != "_baleen"]
        for f in fns:
            p = os.path.join(dp, f)
            outputs[os.path.relpath(p, out).replace("\\", "/")] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    by_out = {r.output_path: r for r in rows.values() if r.output_path}
    assert set(outputs) == set(by_out)
    for rel, sha in outputs.items():
        assert by_out[rel].output_sha256 == sha
    # P5: no staging left behind; output mtime = source mtime
    assert not os.path.exists(os.path.join(out, ".baleen-staging"))
    assert os.stat(os.path.join(out, "a.jpg")).st_mtime_ns == os.stat(os.path.join(src, "a.jpg")).st_mtime_ns
    # records
    rj = json.loads(Path(job.run_json_path()).read_text(encoding="utf-8"))
    assert rj["run_id"] == job.id and rj["cancelled"] is False
    assert rj["resources"]["resolved"]["B"] == min(4, os.cpu_count() or 1)
    assert rj["settings"]["pdfa_level"] == "2b"
    raw = Path(job.report_path()).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    assert not os.path.exists(os.path.join(out, "_baleen", ".lock"))


def test_second_run_resumes_and_changes_nothing(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    make_tree(src)
    run(engine, store, Mode.CONVERT, src, out)
    before = snapshot_tree(out)
    job2 = run(engine, store, Mode.CONVERT, src, out)
    after = snapshot_tree(out)
    rows = rows_by_source(job2)
    for name in ("a.jpg", "b.bmp", "c.doc", "photo.bmp", "sub/e.jpg"):
        assert rows[name].status == "OK" and rows[name].reason == "RESUMED", name
        assert rows[name].output_sha256
    # AC-07: no file changes, no new files (besides the new report/run JSON/journal)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert all(k.startswith("_baleen") for k in changed), changed


def test_output_occupied_never_overwritten(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "a.jpg", b"jpeg-a")
    fakes.write(out, "a.jpg", b"someone else's file")
    job = run(engine, store, Mode.CONVERT, src, out)
    rows = rows_by_source(job)
    assert rows["a.jpg"].status == "NEEDS_REVIEW" and rows["a.jpg"].reason == "OUTPUT_OCCUPIED"
    assert Path(out, "a.jpg").read_bytes() == b"someone else's file"


def test_failed_check_is_never_placed(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "bad.bmp", b"BAD bmp")
    fakes.write(src, "bad.jpg", b"BAD jpeg")
    job = run(engine, store, Mode.CONVERT, src, out)
    rows = rows_by_source(job)
    assert rows["bad.bmp"].status == "FAILED" and rows["bad.bmp"].reason == "VERIFY_FAILED"
    assert rows["bad.jpg"].status == "NEEDS_REVIEW" and rows["bad.jpg"].reason == "SOURCE_INVALID"
    assert not os.path.exists(os.path.join(out, "bad.jpg"))
    assert rows["bad.bmp"].output_path == "" and rows["bad.jpg"].output_path == ""


def test_validator_missing_is_placed_but_flagged(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.VERAPDF["missing"] = True
    fakes.write(src, "c.doc", b"doc")
    job = run(engine, store, Mode.CONVERT, src, out)
    r = rows_by_source(job)["c.doc"]
    assert r.status == "NEEDS_REVIEW" and r.reason == "VALIDATOR_MISSING"
    assert os.path.exists(os.path.join(out, "c.pdf"))
    assert "V-PDFA=unavailable(2b)" in r.checks


def test_not_pdfa_output_fails(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "c.doc", b"NOTPDFA doc")
    job = run(engine, store, Mode.CONVERT, src, out)
    r = rows_by_source(job)["c.doc"]
    assert r.status == "FAILED" and r.reason == "VERIFY_FAILED"
    assert not os.path.exists(os.path.join(out, "c.pdf"))


def test_conversion_error(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "c.doc", b"FAILCONV")
    job = run(engine, store, Mode.CONVERT, src, out)
    r = rows_by_source(job)["c.doc"]
    assert r.status == "FAILED" and r.reason == "CONVERSION_ERROR"


def test_copy_existing_off_checks_in_place(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "a.jpg", b"jpeg")
    job = run(engine, store, Mode.CONVERT, src, out, copy_existing=False)
    r = rows_by_source(job)["a.jpg"]
    assert r.status == "OK" and r.action == "check" and r.output_path == ""
    assert not os.path.exists(os.path.join(out, "a.jpg"))


def test_check_mode_writes_nothing(env) -> None:  # noqa: ANN001
    engine, store, src, _out = env
    make_tree(src)
    before = snapshot_tree(src)
    job = run(engine, store, Mode.CHECK, src, None)
    assert snapshot_tree(src) == before
    rows = rows_by_source(job)
    assert rows["a.jpg"].status == "OK" and rows["a.jpg"].action == "check"
    assert rows["b.bmp"].status == "NEEDS_REVIEW" and rows["b.bmp"].reason == "NOT_ARCHIVAL_FORMAT"
    assert rows["b.bmp"].source_sha256 == hashlib.sha256(b"bmp-b").hexdigest()
    assert "JPEG" in rows["b.bmp"].message
    assert job.report_path().startswith(str(engine.home.reports_dir))
    assert all(r.output_path == "" for r in rows.values())


def test_cancel_skips_the_rest(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    for i in range(30):
        fakes.write(src, f"f{i:02}.jpg", b"SLOW jpeg %d" % i)
    st = copy.deepcopy(store.snapshot())
    st["app"].update({"processor_use": "custom", "processor_cores": 1})
    job = engine.start(RunSpec(Mode.CONVERT, src, out, st), background=True, prefs=lambda: st["app"])
    deadline = time.time() + 10
    while job.done < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert engine.cancel()
    assert job.done_event.wait(30)
    assert job.state == JobState.CANCELLED
    rows = read_csv(job.report_path())
    assert len(rows) == 30
    skipped = [r for r in rows if r.status == "SKIPPED"]
    assert skipped and all(r.reason == "CANCELLED" for r in skipped)
    assert all(not os.path.exists(os.path.join(out, r.source_path)) for r in skipped)
    rj = json.loads(Path(job.run_json_path()).read_text(encoding="utf-8"))
    assert rj["cancelled"] is True


def test_recovery_finalises_interrupted_run(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    make_tree(src)
    job = run(engine, store, Mode.CONVERT, src, out)
    # Simulate a crash: remove the report, keep the journal with only some results.
    os.remove(job.report_path())
    j = Journal.open(job.journal_path())
    conn = j._writer()
    conn.execute("DELETE FROM results WHERE n > 3")
    conn.commit()
    j.close()
    fixed = engine.recover_dir(os.path.join(out, "_baleen"))
    assert job.id in fixed
    rows = read_csv(job.report_path())
    assert len(rows) == job.total
    assert {r.reason for r in rows[3:]} == {"INTERRUPTED"}


def test_lock_held_is_fatal(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "a.jpg", b"j")
    os.makedirs(os.path.join(out, "_baleen"))
    lock = OutputLock(os.path.join(out, "_baleen", ".lock"))
    lock.acquire()  # held by this (live) process
    try:
        with pytest.raises(RootError):
            run(engine, store, Mode.CONVERT, src, out)
    finally:
        lock.release()


def test_stale_lock_replaced(env) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "a.jpg", b"j")
    os.makedirs(os.path.join(out, "_baleen"))
    with open(os.path.join(out, "_baleen", ".lock"), "w") as f:
        json.dump({"pid": 999999, "hostname": socket.gethostname(), "started_at": "x"}, f)
    job = run(engine, store, Mode.CONVERT, src, out)
    assert job.state == JobState.COMPLETED
    assert any("stale lock" in w for w in job.warnings)


@pytest.mark.parametrize("inside", ["out_in_src", "same", "src_in_out"])
def test_overlap_rejected(env, inside) -> None:  # noqa: ANN001
    engine, store, src, out = env
    fakes.write(src, "a.jpg", b"j")
    target = {"out_in_src": os.path.join(src, "OUT"), "same": src.upper() if os.name == "nt" else src,
              "src_in_out": os.path.dirname(src)}[inside]
    with pytest.raises(RootError):
        run(engine, store, Mode.CONVERT, src, target)
    assert not os.path.exists(os.path.join(src, "OUT"))
    assert not os.path.exists(os.path.join(src, "_baleen"))
