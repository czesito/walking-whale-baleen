"""Existing PDFs (spec §6.6, DR-07, DR-08): checked and copied, never converted.

- Encrypted -> NEEDS_REVIEW PDF_ENCRYPTED (plan time).
- XMP pdfaid:part / pdfaid:conformance. No PDF/A claim -> NEEDS_REVIEW NOT_PDFA (plan time),
  not copied.
- A claim is only a request to validate (DR-08): V-PDF-OPEN, then V-PDFA with veraPDF at the
  claimed flavour. Pass -> OK, copied when copy_existing (action copy), otherwise checked in
  place (action check). V-PDFA fail -> NEEDS_REVIEW PDFA_INVALID, not copied.
- Any valid PDF/A flavour is accepted; pdfa_level applies only to new conversions (DR-07).
- A PDF pypdf can't read is routed as a copy, so V-PDF-OPEN fails at run time and the §8
  outcome rule gives SOURCE_INVALID.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: S405 - XMP packet of a local file; no DTD/entity resolution
from typing import ClassVar

from ..model import Action, Category, CheckState, Probe, outcome_from_checks
from ..scheduler import Lane, TaskContext
from ..verify.pdf import v_pdf_open
from .base import ProbeContext, Route, SourceRef, WorkItem, register
from .text import note_check_messages

PDFAID_NS = "http://www.aiim.org/pdfa/ns/id/"
_ATTR_RE = re.compile(rb"""pdfaid:(part|conformance)\s*=\s*["']\s*([A-Za-z0-9]+)\s*["']""")
_ELEM_RE = re.compile(rb"""<pdfaid:(part|conformance)\s*>\s*([A-Za-z0-9]+)\s*<""")
CONFORMANCE = {"1": ("a", "b"), "2": ("a", "b", "u"), "3": ("a", "b", "u")}


def pdfa_claim(xmp: bytes) -> tuple[str | None, str | None]:
    """(part, conformance) claimed by an XMP packet; (None, None) when there is no claim."""
    part = conf = None
    start = xmp.find(b"<")
    try:
        root = ET.fromstring(xmp[start:] if start >= 0 else xmp)  # noqa: S314
        for el in root.iter():
            for k, v in el.attrib.items():
                if k == f"{{{PDFAID_NS}}}part" and part is None:
                    part = v.strip()
                elif k == f"{{{PDFAID_NS}}}conformance" and conf is None:
                    conf = v.strip()
            if el.tag == f"{{{PDFAID_NS}}}part" and part is None:
                part = (el.text or "").strip() or None
            elif el.tag == f"{{{PDFAID_NS}}}conformance" and conf is None:
                conf = (el.text or "").strip() or None
    except ET.ParseError:
        for m in list(_ATTR_RE.finditer(xmp)) + list(_ELEM_RE.finditer(xmp)):
            key, val = m.group(1), m.group(2).decode("ascii")
            if key == b"part" and part is None:
                part = val
            elif key == b"conformance" and conf is None:
                conf = val
    return part, conf


def claim_flavour(part: str | None, conf: str | None) -> tuple[str | None, str]:
    """veraPDF flavour for a claim, or (None, why) when the claim is incomplete or invalid."""
    if not part:
        return None, "no claim"
    c = (conf or "").strip().lower()
    if part == "4":
        return ("4" + c if c in ("e", "f") else "4"), ""
    if part in CONFORMANCE:
        if c in CONFORMANCE[part]:
            return part + c, ""
        if conf:
            return None, f"PDF/A-{part} with conformance level {conf!r}"
        return None, f"PDF/A-{part} without a conformance level"
    return None, f"PDF/A part {part!r}"


class PdfRoute(Route):
    key: ClassVar[str] = "pdf"
    lane: ClassVar[Lane] = Lane.FILES

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        from pypdf import PdfReader

        try:
            with src.open() as f:
                r = PdfReader(f, strict=False)
                version = (r.pdf_header or "").replace("%PDF-", "").strip()
                fmt = f"PDF {version}".strip()
                if r.is_encrypted:
                    return Probe(Category.PDF, ".pdf", Action.COPY, source_format=f"{fmt} (encrypted)",
                                 reasons=["PDF_ENCRYPTED"], message="The PDF is encrypted (password-protected).",
                                 route=self.key)
                meta = r.trailer["/Root"].get_object().get("/Metadata")
                xmp = meta.get_object().get_data() if meta is not None else b""
        except Exception as e:
            # V-PDF-OPEN decides at run time: SOURCE_INVALID (§8).
            return Probe(Category.PDF, ".pdf", Action.COPY, source_format="PDF (unreadable)", method="byte copy",
                         message=f"pypdf can't read it: {e.__class__.__name__}: {e}", route=self.key,
                         data={"unreadable": True})
        part, conf = pdfa_claim(xmp) if xmp else (None, None)
        if not part:
            return Probe(Category.PDF, ".pdf", Action.COPY, source_format=fmt, reasons=["NOT_PDFA"],
                         message="The PDF makes no PDF/A claim in its metadata.", route=self.key)
        flavour, why = claim_flavour(part, conf)
        claimed = f"PDF/A-{part}{(conf or '').upper()}"
        if flavour is None:
            return Probe(Category.PDF, ".pdf", Action.COPY, source_format=f"{fmt}, claims {claimed}",
                         reasons=["PDFA_INVALID"], route=self.key,
                         message=f"The PDF/A claim in its metadata is not valid ({why}).")
        return Probe(Category.PDF, ".pdf", Action.COPY, source_format=f"{fmt}, claims {claimed}",
                     method="byte copy", route=self.key, data={"flavour": flavour})

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        path = work.input_path()
        c = v_pdf_open(path)
        work.checks.append(c)
        if c.state != CheckState.PASS:
            return
        flavour = str(work.plan.data.get("flavour") or "")
        if not flavour:  # unreadable at plan time but opens now: no claim known
            work.fail("NOT_PDFA", "The PDF makes no PDF/A claim that could be read.")
            return
        work.pdfa = (path, flavour)  # DR-08: only veraPDF decides
        if work.resume:
            return
        if work.action == Action.COPY and not work.check_only and work.staged:
            work.result_path = work.staged
            work.method = "byte copy"
        else:
            work.method = "checked in place"

    def decide(self, work: WorkItem) -> list[str]:
        """§6.6: an existing PDF failing V-PDFA is PDFA_INVALID (not copied), not SOURCE_INVALID."""
        note_check_messages(work)
        codes = outcome_from_checks(work.checks, new_output=False)
        opened = all(c.state != CheckState.FAIL for c in work.checks if c.check != "V-PDFA")
        if opened and any(c.check == "V-PDFA" and c.state == CheckState.FAIL for c in work.checks):
            codes = ["PDFA_INVALID"]
        return codes


register(PdfRoute())
