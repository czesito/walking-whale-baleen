"""Existing PDFs (§6.6, DR-07, DR-08): probe, claim parsing, process, decide."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from baleen.convert.pdf import PdfRoute, claim_flavour, pdfa_claim
from baleen.model import Action, CheckResult, CheckState

from .a_support import FakeTaskContext, probe_ctx, run_ctx, src_bytes, work_item

ROUTE = PdfRoute()


def xmp(desc: str, extra_ns: str = 'xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"') -> bytes:
    return (f'<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
            f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" {extra_ns} {desc}</rdf:Description></rdf:RDF></x:xmpmeta>'
            f'<?xpacket end="w"?>').encode()


def make_pdf(meta: bytes | None = None, password: str | None = None) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    w = PdfWriter()
    w.add_blank_page(100, 100)
    if meta is not None:
        s = DecodedStreamObject()
        s.set_data(meta)
        s[NameObject("/Type")] = NameObject("/Metadata")
        s[NameObject("/Subtype")] = NameObject("/XML")
        w.root_object[NameObject("/Metadata")] = w._add_object(s)
    if password:
        w.encrypt(user_password=password, owner_password=password + "-owner", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize(("packet", "claim"), [
    (xmp('pdfaid:part="2" pdfaid:conformance="B">'), ("2", "B")),
    (xmp("><pdfaid:part>1</pdfaid:part><pdfaid:conformance>A</pdfaid:conformance>"), ("1", "A")),
    (xmp('a:part="3" a:conformance="U">', 'xmlns:a="http://www.aiim.org/pdfa/ns/id/"'), ("3", "U")),
    (xmp("><pdfaid:part>4</pdfaid:part>"), ("4", None)),
    (xmp('dc:format="application/pdf">', 'xmlns:dc="http://purl.org/dc/elements/1.1/"'), (None, None)),
    # Wrong namespace URI: not a PDF/A identification.
    (xmp('pdfaid:part="2" pdfaid:conformance="B">', 'xmlns:pdfaid="http://example.invalid/ns/"'), (None, None)),
    (b"<broken <pdfaid:part>2</pdfaid:part> <pdfaid:conformance>b</pdfaid:conformance>", ("2", "b")),
])
def test_pdfa_claim(packet: bytes, claim) -> None:  # noqa: ANN001
    assert pdfa_claim(packet) == claim


@pytest.mark.parametrize(("part", "conf", "flavour"), [
    ("1", "B", "1b"), ("1", "a", "1a"), ("2", "U", "2u"), ("3", "B", "3b"), ("4", None, "4"), ("4", "F", "4f"),
    ("4", "E", "4e"), ("1", "U", None), ("2", None, None), ("5", "B", None), (None, None, None),
])
def test_claim_flavour(part, conf, flavour) -> None:  # noqa: ANN001
    assert claim_flavour(part, conf)[0] == flavour


def test_probe(home) -> None:  # noqa: ANN001
    ctx = probe_ctx(home)
    enc = ROUTE.probe(ctx, src_bytes("e.pdf", make_pdf(password="pw")))
    assert enc.reasons == ["PDF_ENCRYPTED"] and enc.source_format.endswith("(encrypted)")
    plain = ROUTE.probe(ctx, src_bytes("p.pdf", make_pdf()))
    assert plain.reasons == ["NOT_PDFA"] and plain.target_ext == ".pdf"
    claim = ROUTE.probe(ctx, src_bytes("a.pdf", make_pdf(xmp('pdfaid:part="2" pdfaid:conformance="B">'))))
    assert claim.reasons == [] and claim.action == Action.COPY and claim.data == {"flavour": "2b"}
    assert claim.source_format.endswith("claims PDF/A-2B")
    bad_claim = ROUTE.probe(ctx, src_bytes("b.pdf", make_pdf(xmp("><pdfaid:part>2</pdfaid:part>"))))
    assert bad_claim.reasons == ["PDFA_INVALID"] and "conformance" in bad_claim.message
    junk = ROUTE.probe(ctx, src_bytes("j.pdf", b"%PDF-1.4 truncated nonsense"))
    assert junk.reasons == [] and junk.action == Action.COPY and junk.data.get("unreadable")


def _work(home, tmp_path: Path, data: bytes, **kw):  # noqa: ANN001, ANN003, ANN202
    src = tmp_path / "src" / "doc.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    pr = ROUTE.probe(probe_ctx(home), src_bytes(src.name, data))
    return work_item(run_ctx(home, tmp_path, pdfa_level="1b"), src, pr, **kw)


def test_process_copy_requests_claimed_flavour(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, make_pdf(xmp('pdfaid:part="3" pdfaid:conformance="A">')))
    ROUTE.process(FakeTaskContext(), wi)
    assert [c.report_form() for c in wi.checks] == ["V-PDF-OPEN=pass"]
    assert wi.pdfa == (wi.staged, "3a")  # DR-07: the claimed flavour, not pdfa_level
    assert wi.result_path == wi.staged and wi.method == "byte copy" and not wi.new_output


def test_process_check_only_in_place(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, make_pdf(xmp('pdfaid:part="2" pdfaid:conformance="B">')), check_only=True,
               action=Action.CHECK)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.pdfa == (wi.source_abs, "2b") and wi.result_path is None and wi.method == "checked in place"


def test_unreadable_pdf_is_source_invalid(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, b"%PDF-1.4 truncated nonsense")
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.checks[0].state == CheckState.FAIL and wi.pdfa is None
    assert ROUTE.decide(wi) == ["SOURCE_INVALID"]


@pytest.mark.parametrize(("checks", "expect"), [
    ([CheckResult("V-PDF-OPEN", CheckState.PASS), CheckResult("V-PDFA", CheckState.FAIL, "2b", "rule 6.1.3")],
     ["PDFA_INVALID"]),
    ([CheckResult("V-PDF-OPEN", CheckState.PASS), CheckResult("V-PDFA", CheckState.PASS, "2b")], []),
    ([CheckResult("V-PDF-OPEN", CheckState.PASS),
      CheckResult("V-PDFA", CheckState.UNAVAILABLE, "2b", "missing", tool_missing=True)], ["VALIDATOR_MISSING"]),
    ([CheckResult("V-PDF-OPEN", CheckState.PASS), CheckResult("V-PDFA", CheckState.UNAVAILABLE, "2b", "crash")],
     ["VALIDATOR_ERROR"]),
    ([CheckResult("V-PDF-OPEN", CheckState.FAIL)], ["SOURCE_INVALID"]),
])
def test_decide(home, tmp_path: Path, checks, expect) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, make_pdf())
    wi.checks = list(checks)
    assert ROUTE.decide(wi) == expect
    for c in checks:
        if c.state != CheckState.PASS and c.message:
            assert c.message in wi.messages
