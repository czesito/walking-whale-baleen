"""PDF checks (spec §8): V-PDF-OPEN (pypdf), V-PDFA (veraPDF batches, DR-08, DR-35), V-TEXT.

Baseline implementation; owned and hardened by the PDF/A workstream.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET  # noqa: S405 - parsing veraPDF's own report

from .. import proc
from ..model import CheckResult, CheckState
from ..paths import long_path
from ..scheduler import TaskContext


def v_pdf_open(path: str) -> CheckResult:
    """V-PDF-OPEN: opens without a password; page count >= 1."""
    from pypdf import PdfReader

    try:
        r = PdfReader(long_path(path))
        if r.is_encrypted:
            return CheckResult("V-PDF-OPEN", CheckState.FAIL, message="The PDF is encrypted.")
        n = len(r.pages)
    except Exception as e:
        return CheckResult("V-PDF-OPEN", CheckState.FAIL, message=f"pypdf couldn't open it: {e}")
    if n < 1:
        return CheckResult("V-PDF-OPEN", CheckState.FAIL, message="The PDF has no pages.")
    return CheckResult("V-PDF-OPEN", CheckState.PASS)


def verapdf_batch(ctx: TaskContext, run, items: list[tuple[str, str]]) -> list[CheckResult]:  # noqa: ANN001
    """Validate PDFs that share one flavour in one JVM. One CheckResult per item, in order.

    pass <=> validationReport/@isCompliant = "true"; no parsable report -> unavailable (VALIDATOR_ERROR).
    """
    exe = run.tools.path("verapdf")
    java = run.tools.get("java")
    if exe is None or not java.found:
        return [CheckResult("V-PDFA", CheckState.UNAVAILABLE, detail=f, message="veraPDF is not available.",
                            tool_missing=True) for _, f in items]
    flavour = items[0][1]
    timeout = float(run.advanced.get("verapdf_timeout_s", 300)) * max(1, len(items))
    args = [exe, "--format", "xml", "--flavour", flavour, *[p for p, _ in items]]
    res = proc.run(args, timeout=timeout, env=run.tools_env(), low_priority=ctx.low_priority)
    found: dict[str, bool] = {}
    try:
        root = ET.fromstring(res.stdout)  # noqa: S314
        for job in root.iter("job"):
            name_el = job.find("item/name")
            vr = job.find("validationReport")
            if name_el is not None and vr is not None:
                found[os.path.normcase(os.path.abspath(name_el.text or ""))] = vr.get("isCompliant") == "true"
    except ET.ParseError:
        pass
    out: list[CheckResult] = []
    for path, fl in items:
        ok = found.get(os.path.normcase(os.path.abspath(path)))
        if ok is None:
            msg = "veraPDF produced no result." + (" It timed out." if res.timed_out else "")
            out.append(CheckResult("V-PDFA", CheckState.UNAVAILABLE, detail=fl, message=msg))
        else:
            out.append(CheckResult("V-PDFA", CheckState.PASS if ok else CheckState.FAIL, detail=fl,
                                   message="" if ok else "veraPDF reports the file is not compliant."))
    return out
