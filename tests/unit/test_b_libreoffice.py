"""LibreOffice batches (§5.5, §6.2, DR-35, R-12): call planning, retry-alone, profiles (workstream b).

Most tests drive convert_batch() with a simulated soffice (proc.run patched). One test uses the
real LibreOffice, when present, with a corrupt file inside a batch.
"""

from __future__ import annotations

import copy
import io
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from baleen import proc
from baleen import settings as S
from baleen.convert import libreoffice as lo
from baleen.convert.base import LoJob, RunContext
from baleen.home import Home
from baleen.model import Mode
from baleen.tools import Toolset

COMPLETE = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class FakeTools:
    def __init__(self, soffice: str | None = "soffice") -> None:
        self.soffice = soffice

    def path(self, key: str) -> str | None:
        return self.soffice

    def get(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(version="26.8.0.3")

    def env(self) -> dict[str, str]:
        return {}


class FakeSoffice:
    """Simulates `soffice --convert-to`: files are converted in order; content decides the outcome.

    GOOD -> complete PDF; PARTIAL -> a PDF cut off before %%EOF; BAD -> nothing;
    HANG -> the call times out here (files before it are done, later ones never start).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, args: list[str], *, timeout: float | None, env=None, cwd=None, low_priority=False,  # noqa: ANN001
                 input_bytes=None) -> proc.ProcResult:  # noqa: ANN001
        if "--terminate_after_init" in args:
            prof = Path(args[1].split("=", 1)[1].replace("file:///", "")).resolve()
            (prof / "user").mkdir(parents=True, exist_ok=True)
            self.calls.append({"init": True})
            return proc.ProcResult(args, 0)
        outdir = args[args.index("--outdir") + 1]
        inputs = args[args.index("--outdir") + 2:]
        stems = [Path(p).stem.casefold() for p in inputs]
        assert len(set(stems)) == len(stems), "stems must be unique within one call"
        self.calls.append({"inputs": [Path(p).name for p in inputs], "timeout": timeout,
                           "infilter": next((a for a in args if a.startswith("--infilter=")), None)})
        os.makedirs(outdir, exist_ok=True)
        rc = 0
        for p in inputs:
            data = Path(p).read_bytes()
            out = Path(outdir) / (Path(p).stem + ".pdf")
            if data.startswith(b"GOOD"):
                out.write_bytes(COMPLETE)
            elif data.startswith(b"PARTIAL"):
                out.write_bytes(COMPLETE[:20])
                rc = 1
            elif data.startswith(b"HANG"):
                return proc.ProcResult(args, None, timed_out=True)
            else:
                rc = 1
        return proc.ProcResult(args, rc, stderr=b"Error: source file could not be loaded\n" if rc else b"")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    fake = FakeSoffice()
    monkeypatch.setattr(lo.proc, "run", fake)
    home = Home(tmp_path / "home")
    home.ensure()
    run = RunContext("r1", Mode.CONVERT, home, FakeTools(), copy.deepcopy(S.defaults()), str(tmp_path), None,  # type: ignore[arg-type]
                     str(tmp_path / "work"))
    ctx = SimpleNamespace(low_priority=False)
    return fake, run, ctx, tmp_path


def jobs_for(tmp: Path, specs: list[tuple[str, bytes]], infilter: str | None = None) -> list[LoJob]:
    out = []
    for name, data in specs:
        d = tmp / "items" / name.replace(".", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        out.append(LoJob(str(d / name), str(d), "writer_pdf_Export", "2b", infilter))
    return out


def test_plan_calls_limits_and_unique_stems() -> None:
    def j(name: str) -> LoJob:
        return LoJob(f"/w/{name}", "/w", "writer_pdf_Export", "2b")

    assert lo.plan_calls([j(f"d{i}.doc") for i in range(10)]) == [list(range(8)), [8, 9]]
    # stems collide case-insensitively: a second call takes the duplicates, order kept
    calls = lo.plan_calls([j("a/input.doc"), j("b/INPUT.rtf"), j("c/x.doc"), j("d/input.docx")])
    assert calls == [[0, 2], [1], [3]]


def test_pdf_complete(tmp_path: Path) -> None:
    p = tmp_path / "a.pdf"
    p.write_bytes(COMPLETE)
    assert lo.pdf_complete(str(p))
    p.write_bytes(COMPLETE[:-8])
    assert not lo.pdf_complete(str(p))
    p.write_bytes(b"<html>%%EOF")
    assert not lo.pdf_complete(str(p))
    assert not lo.pdf_complete(str(tmp_path / "missing.pdf"))


def test_batch_then_retry_alone(env) -> None:  # noqa: ANN001
    fake, run, ctx, tmp = env
    jobs = jobs_for(tmp, [("doc-1.doc", b"GOOD"), ("doc-2.doc", b"BAD"), ("doc-3.rtf", b"GOOD"),
                          ("doc-4.doc", b"PARTIAL")])
    res = lo.convert_batch(ctx, run, jobs)
    assert [r.ok for r in res] == [True, False, True, False]
    assert [r.retried_alone for r in res] == [False, True, False, True]
    assert res[1].reason == "CONVERSION_ERROR" and "couldn't open" in res[1].message
    assert res[3].reason == "CONVERSION_ERROR"  # a PDF without %%EOF never counts
    assert res[0].output == jobs[0].expected_output and Path(res[0].output).read_bytes() == COMPLETE
    assert res[0].method == "LibreOffice 26.8.0 · writer_pdf_Export · PDF/A-2b"
    calls = [c for c in fake.calls if "inputs" in c]
    assert calls[0]["inputs"] == ["doc-1.doc", "doc-2.doc", "doc-3.rtf", "doc-4.doc"]
    assert calls[0]["timeout"] == 60 + 30 * 4
    assert [c["inputs"] for c in calls[1:]] == [["doc-2.doc"], ["doc-4.doc"]]
    assert all(c["timeout"] == 300 for c in calls[1:])
    assert not list((tmp / "work").glob("lo-batch-*")), "batch folders are removed"


def test_timed_out_batch_keeps_finished_files_and_retries_the_rest(env) -> None:  # noqa: ANN001
    fake, run, ctx, tmp = env
    run.settings["advanced"]["lo_timeout_s"] = 45
    jobs = jobs_for(tmp, [("doc-1.doc", b"GOOD"), ("doc-2.doc", b"HANG"), ("doc-3.doc", b"GOOD")])
    res = lo.convert_batch(ctx, run, jobs)
    assert [r.ok for r in res] == [True, False, True]
    assert res[1].reason == "TIMEOUT" and "45 s" in res[1].message and res[1].retried_alone
    assert res[2].retried_alone and not res[0].retried_alone
    calls = [c["inputs"] for c in fake.calls if "inputs" in c]
    assert calls == [["doc-1.doc", "doc-2.doc", "doc-3.doc"], ["doc-2.doc"], ["doc-3.doc"]]


def test_single_file_uses_per_file_limit(env) -> None:  # noqa: ANN001
    fake, run, ctx, tmp = env
    res = lo.convert_batch(ctx, run, jobs_for(tmp, [("mail-7.html", b"GOOD")], infilter="HTML (StarWriter)"))
    assert res[0].ok and not res[0].retried_alone
    call = [c for c in fake.calls if "inputs" in c][0]
    assert call["timeout"] == 300 and call["infilter"] == "--infilter=HTML (StarWriter)"


def test_more_than_eight_and_colliding_stems(env) -> None:  # noqa: ANN001
    fake, run, ctx, tmp = env
    specs = [(f"doc-{i}.doc", b"GOOD") for i in range(9)]
    jobs = jobs_for(tmp, specs)
    clash = tmp / "other"
    clash.mkdir()
    (clash / "doc-0.rtf").write_bytes(b"GOOD")
    jobs.append(LoJob(str(clash / "doc-0.rtf"), str(clash), "writer_pdf_Export", "2b"))
    res = lo.convert_batch(ctx, run, jobs)
    assert all(r.ok for r in res) and len(res) == 10
    sizes = [len(c["inputs"]) for c in fake.calls if "inputs" in c]
    assert sizes == [8, 2]
    assert res[9].output == str(clash / "doc-0.pdf") and res[0].output != res[9].output


def test_mixed_batch_keys_rejected(env) -> None:  # noqa: ANN001
    _fake, run, ctx, tmp = env
    a, b = jobs_for(tmp, [("doc-1.doc", b"GOOD"), ("doc-2.doc", b"GOOD")])
    b.pdfa_level = "1b"
    with pytest.raises(ValueError):
        lo.convert_batch(ctx, run, [a, b])


def test_tool_missing(env) -> None:  # noqa: ANN001
    _fake, run, ctx, tmp = env
    run.tools = FakeTools(None)  # type: ignore[assignment]
    res = lo.convert_batch(ctx, run, jobs_for(tmp, [("doc-1.doc", b"GOOD")]))
    assert res[0].reason == "TOOL_MISSING" and not res[0].ok


def test_profile_initialised_once_with_settings(env) -> None:  # noqa: ANN001
    fake, run, ctx, tmp = env
    lo.convert_batch(ctx, run, jobs_for(tmp, [("doc-1.doc", b"GOOD")]))
    lo.convert_batch(ctx, run, jobs_for(tmp, [("doc-2.doc", b"GOOD")]))
    assert sum(1 for c in fake.calls if c.get("init")) == 1
    prof = run.home.lo_profile(0)
    assert (prof / "baleen-profile-ok").exists()
    assert lo._settings_ok(prof / "user" / "registrymodifications.xcu")


def test_write_settings_merges_and_repairs(tmp_path: Path) -> None:
    prof = tmp_path / "p"
    xcu = prof / "user" / "registrymodifications.xcu"
    xcu.parent.mkdir(parents=True)
    xcu.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
                   'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
                   '<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="SymbolStyle" oor:op="fuse">'
                   '<value>colibre</value></prop></item>\n'
                   '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop '
                   'oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>false</value></prop></item>\n'
                   "</oor:items>\n", encoding="utf-8")
    assert not lo._settings_ok(xcu)
    lo.write_settings(prof)
    assert lo._settings_ok(xcu)
    root = ET.parse(xcu).getroot()
    text = ET.tostring(root, encoding="unicode")
    assert "colibre" in text, "other settings are kept"
    assert text.count("BlockUntrustedRefererLinks") == 1
    before = xcu.read_bytes()
    lo.write_settings(prof)
    assert xcu.read_bytes() == before, "idempotent"


def test_profile_pool_lock_across_pools(tmp_path: Path) -> None:
    """Two pools (as two Baleen processes sharing a BALEEN_HOME) never hand out the same profile."""
    a, b = lo.ProfilePool(), lo.ProfilePool()
    ka = a.acquire(tmp_path)
    kb = b.acquire(tmp_path)
    assert (ka, kb) == (0, 1)
    a.release(ka)
    assert b.acquire(tmp_path) == 0
    assert a.acquire(None) == 0  # in-process only when no lock folder is given


def test_short_version() -> None:
    assert lo.short_version("26.8.0.3") == "26.8.0" and lo.short_version("") == ""


# --------------------------------------------------------------------------- real LibreOffice


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/'
                   'word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
                   'document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   "</Relationships>")
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.'
                   'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic DOCX for Baleen.'
                   "</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def test_real_libreoffice_corrupt_file_in_a_batch(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """R-12: a corrupt file inside a batch fails alone; its neighbours are converted by the batch."""
    home = Home(tmp_path / "home")
    home.ensure()
    tools = Toolset(home)
    if tools.path("libreoffice") is None:
        pytest.skip("LibreOffice not available")
    run = RunContext("r1", Mode.CONVERT, home, tools, copy.deepcopy(S.defaults()), str(tmp_path), None,
                     str(tmp_path / "work"))
    rtf = rb"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0 Synthetic RTF %d.\par}"
    docx = _docx_bytes()
    jobs = jobs_for(tmp_path, [("doc-1.rtf", rtf.replace(b"%d", b"1")), ("doc-2.docx", docx[: len(docx) // 2]),
                               ("doc-3.docx", docx), ("doc-4.rtf", rtf.replace(b"%d", b"4"))])
    calls: list[list[str]] = []
    real_run = proc.run

    def spy(args, **kw):  # noqa: ANN001, ANN003, ANN202
        if "--outdir" in args:
            calls.append([Path(a).name for a in args[args.index("--outdir") + 2:]])
        return real_run(args, **kw)

    monkeypatch.setattr(lo.proc, "run", spy)
    t0 = time.monotonic()
    res = lo.convert_batch(SimpleNamespace(low_priority=True), run, jobs)  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0
    assert [r.ok for r in res] == [True, False, True, True], [r.message for r in res]
    assert res[1].reason == "CONVERSION_ERROR" and res[1].retried_alone
    assert not any(r.retried_alone for r in (res[0], res[2], res[3]))
    assert calls == [["doc-1.rtf", "doc-2.docx", "doc-3.docx", "doc-4.rtf"], ["doc-2.docx"]]
    for r in (res[0], res[2], res[3]):
        assert lo.pdf_complete(r.output or "")
    # The security settings survive LibreOffice's own rewrite of the file on exit. (It resets the
    # update-check flag on start; ensure_profile() writes it again before every call.)
    xcu = home.lo_profile(0) / "user" / "registrymodifications.xcu"
    root = ET.parse(xcu).getroot()
    oor = "{http://openoffice.org/2001/registry}"
    have = {(i.get(oor + "path"), p.get(oor + "name")): (p.findtext("value")) for i in root for p in i}
    for path, name, value in lo.PROFILE_SETTINGS:
        if name != "AutoCheckEnabled":
            assert have.get((path, name)) == value, name
    lo.write_settings(home.lo_profile(0))
    assert lo._settings_ok(xcu)
    assert elapsed < 120
    # P1-style hygiene: no lock files or batch folders left in the work area
    leftovers = [p.name for p in (tmp_path / "items").rglob("*") if p.name.startswith(".~lock")]
    assert leftovers == [] and not list((tmp_path / "work").glob("lo-batch-*"))
