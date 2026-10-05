"""Plain text: §6.3 encoding detection, staging copy, decide(), and V-TEXT."""

from __future__ import annotations

import collections
import io
from pathlib import Path

import pytest

from baleen.convert.base import LoJob, LoResult
from baleen.convert.text import TEXT_INFILTER, TextRoute, normalise_text, settle
from baleen.model import Action, CheckResult, CheckState, Mode
from baleen.verify import pdf as vpdf

from .a_support import FakeTaskContext, probe_ctx, run_ctx, src_bytes, work_item

ROUTE = TextRoute()
CJK = "訪談紀錄：合成測試\n日本語、한국어, English.\n"


def opener(data: bytes):  # noqa: ANN201
    return lambda: io.BytesIO(data)


# --------------------------------------------------------------------------- §6.3


@pytest.mark.parametrize(("data", "codec", "label"), [
    (b"\xef\xbb\xbf" + CJK.encode(), "utf-8-sig", "UTF-8, BOM"),
    (b"\xff\xfe" + CJK.encode("utf-16-le"), "utf-16", "UTF-16 LE, BOM"),
    (b"\xfe\xff" + CJK.encode("utf-16-be"), "utf-16", "UTF-16 BE, BOM"),
    (CJK.encode("utf-32"), "utf-32", "UTF-32 LE, BOM"),
    (CJK.encode("utf-8"), "utf-8", "UTF-8"),
    (b"plain ascii\r\n", "utf-8", "ASCII"),
    (b"", "utf-8", "ASCII"),
])
def test_certain_encodings(data, codec, label) -> None:  # noqa: ANN001
    for setting in ("auto", "big5"):  # a certain file ignores the setting
        enc = settle(opener(data), setting)
        assert (enc.codec, enc.label, enc.reason) == (codec, label, None)


@pytest.mark.parametrize(("data", "hint"), [
    ("舊檔案的繁體中文".encode("big5"), "Big5"),
    ("😀".encode("gb18030"), "GB18030"),  # 4-byte GB18030: never valid Big5
    ("ｱｲｳ".encode("cp932"), "Shift_JIS"),  # half-width katakana, odd byte count
    ("Café crème".encode("cp1252"), "Windows-1252"),
])
def test_uncertain_under_auto_names_a_hint(data, hint) -> None:  # noqa: ANN001
    enc = settle(opener(data), "auto")
    assert enc.reason == "ENCODING_UNCERTAIN" and enc.codec is None
    assert f"decodes as {hint}" in enc.message


def test_uncertain_without_any_hint() -> None:
    enc = settle(opener(b"\x81"), "auto")
    assert enc.reason == "ENCODING_UNCERTAIN" and "doesn't decode as Big5, GB18030, Shift_JIS or Windows-1252" in (
        enc.message)


def test_named_encoding() -> None:
    big5 = "舊檔案的繁體中文".encode("big5")
    enc = settle(opener(big5), "big5")
    assert enc.reason is None and enc.codec == "cp950" and enc.label == "Big5, chosen in Options"
    bad = settle(opener("Café".encode("cp1252")), "big5")
    assert bad.reason == "ENCODING_MISMATCH" and "0xE9 at offset 3" in bad.message
    assert settle(opener("ｱｲｳ".encode("cp932")), "shift_jis").codec == "cp932"


def test_bom_with_invalid_content_is_not_certain() -> None:
    enc = settle(opener(b"\xff\xfeA"), "auto")  # UTF-16 BOM, then an odd trailing byte
    assert enc.reason == "ENCODING_UNCERTAIN"


def test_strict_decoding_is_streamed(monkeypatch) -> None:  # noqa: ANN001
    from baleen.convert import text

    monkeypatch.setattr(text, "CHUNK", 3)  # multi-byte sequences split across chunks
    assert settle(opener(CJK.encode()), "auto").codec == "utf-8"


def test_normalise_text() -> None:
    assert normalise_text("﻿a\r\nb\rc\n") == "a\nb\nc\n"


def test_probe_modes(home) -> None:  # noqa: ANN001
    big5 = "繁體".encode("big5")
    pr = ROUTE.probe(probe_ctx(home), src_bytes("a.txt", big5))
    assert pr.reasons == ["ENCODING_UNCERTAIN"] and pr.method == ""
    named = ROUTE.probe(probe_ctx(home, txt_encoding="big5"), src_bytes("a.txt", big5))
    assert named.reasons == [] and named.data["codec"] == "cp950"
    assert named.source_format == "Plain text (Big5, chosen in Options)"
    # Check mode has no options: the planner reports NOT_ARCHIVAL_FORMAT.
    chk = ROUTE.probe(probe_ctx(home, Mode.CHECK), src_bytes("a.txt", big5))
    assert chk.reasons == [] and chk.action == Action.CONVERT and chk.target_ext == ".pdf"


# --------------------------------------------------------------------------- Documents lane


def _work(home, tmp_path: Path, data: bytes, **wf):  # noqa: ANN001, ANN003, ANN202
    src = tmp_path / "src" / "notes.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    pr = ROUTE.probe(probe_ctx(home, **wf), src_bytes(src.name, data))
    return work_item(run_ctx(home, tmp_path, **wf), src, pr, n=7)


def test_prepare_writes_utf8_bom_lf_staging_copy(home, tmp_path: Path) -> None:  # noqa: ANN001
    data = "第一行\r\n第二行\rthird\n".encode("utf-16")
    wi = _work(home, tmp_path, data, pdfa_level="3b")
    job = ROUTE.prepare(FakeTaskContext(), wi)
    assert isinstance(job, LoJob)
    assert Path(job.input_path).name == "lo_7.txt"  # unique stem per batch
    assert Path(job.input_path).read_bytes() == b"\xef\xbb\xbf" + "第一行\n第二行\nthird\n".encode()
    assert job.infilter == TEXT_INFILTER == "Text (encoded):UTF8,LF,,,"
    assert (job.export_filter, job.pdfa_level) == ("writer_pdf_Export", "3b")
    assert ROUTE.batch_key(wi) == f"{TEXT_INFILTER}|writer_pdf_Export|3b"
    assert sum(wi.extra["source_chars"].values()) == 11
    assert Path(job.expected_output) == Path(wi.work_dir) / "lo_7.pdf"


def test_prepare_named_encoding(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, "繁體中文".encode("big5"), txt_encoding="big5")
    job = ROUTE.prepare(FakeTaskContext(), wi)
    assert Path(job.input_path).read_bytes() == b"\xef\xbb\xbf" + "繁體中文".encode()


def test_finish_failure_reasons(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, b"hello")
    job = ROUTE.prepare(FakeTaskContext(), wi)
    ROUTE.finish(FakeTaskContext(), wi, LoResult(job, False, None, "TIMEOUT", "too slow", method="LibreOffice x"))
    assert wi.done and wi.reasons == ["TIMEOUT"] and wi.method.startswith("LibreOffice x")


def _fake_pdf(tmp_path: Path) -> str:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(100, 100)
    p = tmp_path / "out.pdf"
    with open(p, "wb") as f:
        w.write(f)
    return str(p)


def test_finish_runs_checks_and_requests_pdfa(home, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, CJK.encode())
    job = ROUTE.prepare(FakeTaskContext(), wi)
    monkeypatch.setattr(vpdf, "extract_text", lambda p: CJK)
    pdf = _fake_pdf(tmp_path)
    ROUTE.finish(FakeTaskContext(), wi, LoResult(job, True, pdf, method="LibreOffice 26.8"))
    assert [c.report_form() for c in wi.checks] == ["V-PDF-OPEN=pass", "V-TEXT=pass"]
    assert wi.pdfa == (pdf, "2b") and wi.new_output and wi.result_path == pdf and wi.action == Action.CONVERT
    assert wi.method == "LibreOffice 26.8 · text decoded as UTF-8"


def test_text_loss_skips_pdfa(home, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, CJK.encode())
    job = ROUTE.prepare(FakeTaskContext(), wi)
    monkeypatch.setattr(vpdf, "extract_text", lambda p: "English.")
    ROUTE.finish(FakeTaskContext(), wi, LoResult(job, True, _fake_pdf(tmp_path)))
    assert wi.checks[-1].state == CheckState.FAIL and wi.pdfa is None
    assert ROUTE.decide(wi) == ["TEXT_LOSS"] and "Missing, for example" in wi.messages[0]


@pytest.mark.parametrize(("checks", "new", "expect"), [
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "fail")], True, ["TEXT_LOSS"]),
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "fail"), ("V-PDFA", "fail")], True, ["TEXT_LOSS", "VERIFY_FAILED"]),
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "pass"), ("V-PDFA", "fail")], True, ["VERIFY_FAILED"]),
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "unavailable"), ("V-PDFA", "pass")], True, ["VALIDATOR_ERROR"]),
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "fail")], False, ["SOURCE_INVALID"]),  # resume -> OUTPUT_INVALID
    ([("V-PDF-OPEN", "pass"), ("V-TEXT", "pass"), ("V-PDFA", "pass")], True, []),
])
def test_decide(home, tmp_path: Path, checks, new, expect) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, b"x")
    wi.new_output = new
    wi.checks = [CheckResult(c, CheckState(s)) for c, s in checks]
    assert ROUTE.decide(wi) == expect


def test_resume_compares_with_source_in_place(home, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, CJK.encode())
    wi.staged = None
    wi.resume, wi.existing_output = True, _fake_pdf(tmp_path)
    monkeypatch.setattr(vpdf, "extract_text", lambda p: CJK)
    ROUTE.check_existing(FakeTaskContext(), wi)
    assert [c.report_form() for c in wi.checks] == ["V-PDF-OPEN=pass", "V-TEXT=pass"]
    assert wi.pdfa == (wi.existing_output, "2b")


def test_resume_of_an_attachment_uses_its_staged_copy(home, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, CJK.encode())  # staged copy exists, as the e-mail stages it
    wi.source_abs = None
    wi.resume, wi.existing_output = True, _fake_pdf(tmp_path)
    monkeypatch.setattr(vpdf, "extract_text", lambda p: CJK)
    ROUTE.check_existing(FakeTaskContext(), wi)
    assert [c.report_form() for c in wi.checks] == ["V-PDF-OPEN=pass", "V-TEXT=pass"]


# --------------------------------------------------------------------------- V-TEXT


@pytest.mark.parametrize(("extracted", "state"), [
    (CJK, CheckState.PASS),
    (CJK.replace("\n", " ").replace(" ", ""), CheckState.PASS),  # whitespace never counts
    (CJK.encode("utf-8").decode("cp1252", "replace"), CheckState.FAIL),  # mojibake: more chars, wrong ones
    ("", CheckState.FAIL),
])
def test_v_text_states(monkeypatch, extracted, state) -> None:  # noqa: ANN001
    monkeypatch.setattr(vpdf, "extract_text", lambda p: extracted)
    assert vpdf.v_text("x.pdf", vpdf.visible_chars(CJK)).state == state


def test_v_text_threshold(monkeypatch) -> None:  # noqa: ANN001
    src = "a" * 98 + "b" * 2
    monkeypatch.setattr(vpdf, "extract_text", lambda p: "a" * 98)
    assert vpdf.v_text("x", vpdf.visible_chars(src)).state == CheckState.PASS  # exactly 98%
    monkeypatch.setattr(vpdf, "extract_text", lambda p: "a" * 97)
    r = vpdf.v_text("x", vpdf.visible_chars(src))
    assert r.state == CheckState.FAIL and "97.0%" in r.message and "b (U+0062)" in r.message


def test_v_text_normalises_ligatures_and_ignores_controls(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vpdf, "extract_text", lambda p: "ﬁne ＡＢＣ")
    assert vpdf.v_text("x", vpdf.visible_chars("fine ABC\x07​­")).state == CheckState.PASS


def test_v_text_empty_source_and_errors(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(vpdf, "extract_text", lambda p: "")
    assert vpdf.v_text("x", collections.Counter()).state == CheckState.PASS

    def boom(p: str) -> str:
        raise ValueError("bad font")

    monkeypatch.setattr(vpdf, "extract_text", boom)
    assert vpdf.v_text("x", vpdf.visible_chars("abc")).state == CheckState.UNAVAILABLE
