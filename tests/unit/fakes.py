"""Fake routes for engine tests: deterministic, no external tools.

Content conventions in fixture files:
    b"BAD"      at the start  -> the route's check fails (V-IMG fail / VERIFY_FAILED)
    b"NOTPDFA"  anywhere      -> fake veraPDF reports not compliant
    b"SLOW"     at the start  -> processing sleeps 0.3 s (cancel tests)
"""

from __future__ import annotations

import os
import shutil
import time
from typing import ClassVar

from baleen.convert import base, libreoffice
from baleen.convert.base import LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem
from baleen.model import Action, Category, CheckResult, CheckState, Probe
from baleen.paths import long_path
from baleen.scheduler import Lane, TaskContext


def _read(path: str) -> bytes:
    with open(long_path(path), "rb") as f:
        return f.read()


class FakeImage(Route):
    key: ClassVar[str] = "image"
    lane: ClassVar[Lane] = Lane.FILES

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        if src.ext in (".jpg", ".jpeg"):
            return Probe(Category.IMAGE, ".jpg", Action.COPY, "JPEG", route="image")
        return Probe(Category.IMAGE, ".jpg", Action.CONVERT, src.ext.upper().lstrip("."), method="fake convert",
                     route="image")

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        data = _read(work.input_path())
        if data.startswith(b"SLOW"):
            time.sleep(0.3)
        if work.action == Action.CONVERT and not work.resume:
            out = work.out("out.jpg")
            with open(long_path(out), "wb") as f:
                f.write(b"converted:" + data)
            work.result_path = out
            work.new_output = True
            data = b"converted:" + data
        elif work.action == Action.COPY:
            work.result_path = work.staged
        bad = data.startswith(b"BAD") or data.startswith(b"converted:BAD")
        work.checks.append(CheckResult("V-IMG", CheckState.FAIL if bad else CheckState.PASS))


class FakeDocument(Route):
    key: ClassVar[str] = "document"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    batched: ClassVar[bool] = True
    converter_tools: ClassVar[tuple[str, ...]] = ()

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        return Probe(Category.DOCUMENT, ".pdf", Action.CONVERT, "Word", route="document")

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        inp = work.out(f"lo_{work.n}{work.plan.ext}")
        shutil.copyfile(long_path(work.input_path()), long_path(inp))
        return LoJob(inp, work.work_dir, "writer_pdf_Export", work.settings["pdfa_level"])

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        if not result.ok:
            work.fail(result.reason or "CONVERSION_ERROR", result.message)
            return
        work.result_path = result.output
        work.new_output = True
        work.action = Action.CONVERT
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))
        work.pdfa = (result.output or "", work.settings["pdfa_level"])

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))
        work.pdfa = (work.input_path(), work.settings["pdfa_level"])


def fake_convert_batch(ctx, run, jobs):  # noqa: ANN001, ANN201
    out = []
    for j in jobs:
        data = _read(j.input_path)
        if data.startswith(b"FAILCONV"):
            out.append(LoResult(j, False, None, "CONVERSION_ERROR", "fake failure"))
            continue
        dest = j.expected_output
        with open(long_path(dest), "wb") as f:
            f.write(b"%PDF-fake\n" + data)
        out.append(LoResult(j, True, dest, method="FakeOffice 1.0"))
    FAKE_STATS["batches"].append(len(jobs))
    return out


FAKE_STATS: dict[str, list[int]] = {"batches": []}
VERAPDF = {"missing": False}


def fake_verapdf_batch(ctx, run, items):  # noqa: ANN001, ANN201
    res = []
    for path, flavour in items:
        if VERAPDF["missing"]:
            res.append(CheckResult("V-PDFA", CheckState.UNAVAILABLE, flavour, "missing", tool_missing=True))
        elif b"NOTPDFA" in _read(path):
            res.append(CheckResult("V-PDFA", CheckState.FAIL, flavour))
        else:
            res.append(CheckResult("V-PDFA", CheckState.PASS, flavour))
    return res


def install(monkeypatch) -> None:  # noqa: ANN001
    from baleen.verify import pdf as vpdf

    monkeypatch.setattr(base, "_registry", {"image": FakeImage(), "document": FakeDocument()})
    monkeypatch.setattr(base, "_loaded", True)
    monkeypatch.setattr(libreoffice, "convert_batch", fake_convert_batch)
    monkeypatch.setattr(vpdf, "verapdf_batch", fake_verapdf_batch)
    VERAPDF["missing"] = False
    FAKE_STATS["batches"].clear()


def write(root: str, rel: str, data: bytes) -> str:
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p
