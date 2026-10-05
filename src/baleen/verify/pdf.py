"""PDF checks (spec §8): V-PDF-OPEN (pypdf), V-TEXT (pypdf), V-PDFA (veraPDF batches).

V-PDFA (DR-08, DR-35, §5.5):
- PDFs that share a flavour are validated in one JVM per batch (the scheduler groups up to
  25, waits up to 10 s, and closes early when idle).
- pass <=> validationReport/@isCompliant = "true". A file veraPDF cannot parse fails; a
  report veraPDF couldn't finish, or no parsable report at all, is "unavailable"
  (VALIDATOR_ERROR). A missing tool is "unavailable" with tool_missing (VALIDATOR_MISSING).
- If a batch run crashes, times out or leaves files without a result, those files are
  revalidated one by one, each with `verapdf_timeout_s`.
- R-02: every file is linked or copied into a private batch folder under an ASCII name
  (0001.pdf ...) and veraPDF runs with that folder as its working directory, so only short
  ASCII arguments reach veraPDF. Paths with spaces, CJK or shell metacharacters never pass
  through verapdf.bat / cmd.exe or the JVM's ANSI command line, and results are matched
  per file by those names. Sources are never hard-linked (P1); only Baleen's own work
  files are.
"""

from __future__ import annotations

import collections
import itertools
import os
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - parses veraPDF's own report (no DTDs, no entities)
from dataclasses import dataclass
from typing import Any

from .. import proc
from ..model import CheckResult, CheckState, clip_message
from ..paths import is_within, long_path
from ..scheduler import TaskContext

TEXT_MIN_RATIO = 0.98  # §6.3 V-TEXT
BATCH_PER_FILE_S = 30  # batch timeout = verapdf_timeout_s + 30 s per additional file
_batch_ids = itertools.count(1)


# --------------------------------------------------------------------------- V-PDF-OPEN


def v_pdf_open(path: str) -> CheckResult:
    """V-PDF-OPEN: opens without a password; page count >= 1."""
    from pypdf import PdfReader

    try:
        with open(long_path(path), "rb") as f:
            r = PdfReader(f, strict=False)
            if r.is_encrypted:
                return CheckResult("V-PDF-OPEN", CheckState.FAIL, message="The PDF is encrypted.")
            n = len(r.pages)
    except MemoryError:
        return CheckResult("V-PDF-OPEN", CheckState.UNAVAILABLE, message="Not enough memory to open the PDF.")
    except Exception as e:
        return CheckResult("V-PDF-OPEN", CheckState.FAIL,
                           message=f"pypdf couldn't open it: {e.__class__.__name__}: {e}")
    if n < 1:
        return CheckResult("V-PDF-OPEN", CheckState.FAIL, message="The PDF has no pages.")
    return CheckResult("V-PDF-OPEN", CheckState.PASS)


# --------------------------------------------------------------------------- V-TEXT

_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})


def visible_chars(text: str) -> collections.Counter[str]:
    """Non-whitespace characters of `text` as a multiset (NFKC, so ligatures and width
    variants compare equal). Control and format characters can't be extracted from a PDF
    and are not counted."""
    norm = unicodedata.normalize("NFKC", text)
    return collections.Counter(ch for ch in norm if not ch.isspace() and unicodedata.category(ch) not in _INVISIBLE)


def extract_text(path: str) -> str:
    from pypdf import PdfReader

    with open(long_path(path), "rb") as f:
        r = PdfReader(f, strict=False)
        return "\n".join((p.extract_text() or "") for p in r.pages)


def v_text(pdf_path: str, source: collections.Counter[str]) -> CheckResult:
    """V-TEXT (§6.3): the source's non-whitespace characters found in the PDF >= 98%.

    Characters are matched by identity (multiset), not just counted: mis-decoded text
    (mojibake) has more characters than the source and must not pass.
    """
    total = sum(source.values())
    try:
        got = visible_chars(extract_text(pdf_path))
    except MemoryError:
        return CheckResult("V-TEXT", CheckState.UNAVAILABLE, message="Not enough memory to extract the text.")
    except Exception as e:
        return CheckResult("V-TEXT", CheckState.UNAVAILABLE,
                           message=f"pypdf couldn't extract the text: {e.__class__.__name__}: {e}")
    if total == 0:
        return CheckResult("V-TEXT", CheckState.PASS)
    matched = sum(min(n, got.get(ch, 0)) for ch, n in source.items())
    ratio = matched / total
    if ratio >= TEXT_MIN_RATIO:
        return CheckResult("V-TEXT", CheckState.PASS)
    missing = sorted(((n - got.get(ch, 0), ch) for ch, n in source.items() if got.get(ch, 0) < n), reverse=True)
    sample = " ".join(f"{ch} (U+{ord(ch):04X})" for _, ch in missing[:5])
    return CheckResult("V-TEXT", CheckState.FAIL,
                       message=f"Only {ratio:.1%} of the text's characters are in the PDF (98% needed). "
                               f"Missing, for example: {sample}.")


# --------------------------------------------------------------------------- veraPDF report


@dataclass
class JobOutcome:
    state: CheckState
    message: str = ""


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el.iter():
        if c is not el and _local(c.tag) == name:
            return c
    return None


def _basename(name: str) -> str:
    return re.split(r"[\\/]", name.strip())[-1].casefold()


def _failed_rules(vr: ET.Element, limit: int = 3) -> str:
    rules = []
    total = 0
    for r in vr.iter():
        if _local(r.tag) != "rule" or (r.get("status") or "").lower() != "failed":
            continue
        total += 1
        if len(rules) < limit:
            desc = _child(r, "description")
            text = " ".join((desc.text or "").split()) if desc is not None else ""
            clause = r.get("clause") or ""
            rules.append(f"{clause} {text}".strip())
    if not total:
        return ""
    more = f" (+{total - len(rules)} more)" if total > len(rules) else ""
    return f" Failed rules: {'; '.join(rules)}{more}."


def _job_outcome(job: ET.Element) -> tuple[str | None, JobOutcome | None]:
    item = _child(job, "item")
    name_el = _child(item, "name") if item is not None else None
    if name_el is None:
        name_el = _child(job, "name")
    name = (name_el.text or "").strip() if name_el is not None else ""
    if not name:
        return None, None
    vr = _child(job, "validationReport")
    if vr is not None:
        end = (vr.get("jobEndStatus") or "normal").lower()
        compliant = (vr.get("isCompliant") or "").lower()
        profile = vr.get("profileName") or ""
        if end != "normal":
            return name, JobOutcome(CheckState.UNAVAILABLE, f"veraPDF did not finish validating ({end}).")
        if compliant == "true":
            return name, JobOutcome(CheckState.PASS)
        if compliant == "false":
            target = profile.replace(" validation profile", "") or "the PDF/A profile"
            return name, JobOutcome(CheckState.FAIL, f"veraPDF: not compliant with {target}.{_failed_rules(vr)}")
        return name, JobOutcome(CheckState.UNAVAILABLE, "veraPDF's report has no compliance result.")
    te = _child(job, "taskException")
    if te is not None:
        kind = (te.get("type") or "").upper()
        msg_el = _child(te, "exceptionMessage")
        detail = " ".join((msg_el.text or "").split()) if msg_el is not None else ""
        if kind == "PARSE":
            return name, JobOutcome(CheckState.FAIL, f"veraPDF couldn't parse the file: {detail}".strip())
        return name, JobOutcome(CheckState.UNAVAILABLE, f"veraPDF failed ({kind or 'error'}): {detail}".strip())
    return name, None


def parse_report(data: bytes) -> dict[str, JobOutcome]:
    """Results per file from a veraPDF XML report, keyed by case-folded base name.

    Tolerates text before the XML and a truncated document (crash, kill on timeout): every
    <job> that was written completely is kept.
    """
    out: dict[str, JobOutcome] = {}
    starts = [i for i in (data.find(b"<?xml"), data.find(b"<report")) if i >= 0]
    if not starts:
        return out
    body = data[min(starts):]
    parser = ET.XMLPullParser(events=("end",))
    parser.feed(body)  # a syntax error is queued as an event, not raised here
    try:
        parser.close()
    except ET.ParseError:
        pass  # truncated: keep the jobs completed before the break
    try:
        for _ev, el in parser.read_events():  # raises when it reaches a queued syntax error
            if _local(el.tag) != "job":
                continue
            name, outcome = _job_outcome(el)
            if name and outcome is not None:
                out[_basename(name)] = outcome
    except ET.ParseError:
        pass
    return out


# --------------------------------------------------------------------------- veraPDF runs


def _unavailable(flavour: str, message: str, tool_missing: bool = False) -> CheckResult:
    return CheckResult("V-PDFA", CheckState.UNAVAILABLE, detail=flavour, message=clip_message(message),
                       tool_missing=tool_missing)


def _env(run: Any) -> dict[str, str]:
    env = dict(run.tools_env())
    # The veraPDF launcher runs "java" from PATH unless JAVACMD is set: always use the
    # detected (bundled) Java, never whatever is on PATH.
    java = run.tools.path("java")
    if java:
        env.setdefault("JAVACMD", java)  # tools_env sets it too
    env["JAVA_OPTS"] = "-Xmx1g"  # §5.5: the PDF/A lane reserves 1 GB for its JVM
    return env


def _run_verapdf(ctx: TaskContext, run: Any, exe: str, flavour: str, names: list[str], cwd: str,
                 timeout: float) -> tuple[dict[str, JobOutcome], proc.ProcResult]:
    args = [exe, "--format", "xml", "--flavour", flavour, *names]
    res = proc.run(args, timeout=timeout, env=_env(run), cwd=cwd, low_priority=ctx.low_priority)
    return parse_report(res.stdout), res


def _why(res: proc.ProcResult, timeout: float) -> str:
    if res.error:
        return f"veraPDF couldn't start: {res.error}"
    if res.timed_out:
        return f"veraPDF took longer than {int(timeout)} s and was stopped."
    tail = (res.err() or res.out()).strip()
    tail = tail[-300:] if tail else ""
    return f"veraPDF produced no result (exit code {res.returncode}).{(' ' + tail) if tail else ''}"


def _place(src: str, dst: str, run: Any, ctx: TaskContext) -> None:
    """Hard-link Baleen's own work files (cheap); copy anything else (sources, outputs) - a
    source is never linked, which would change its link count (P1)."""
    if is_within(src, run.work_root, resolve=False):
        try:
            os.link(long_path(src), long_path(dst))
            return
        except OSError:
            pass
        shutil.copyfile(long_path(src), long_path(dst))
        return
    with ctx.transfer():
        shutil.copyfile(long_path(src), long_path(dst))


def _validate_group(ctx: TaskContext, run: Any, exe: str, flavour: str, members: list[tuple[int, str]],
                    results: list[CheckResult | None]) -> None:
    single = float(run.advanced.get("verapdf_timeout_s", 300))
    batch_dir = os.path.join(run.work_root, f"verapdf-{next(_batch_ids)}")
    os.makedirs(long_path(batch_dir), exist_ok=True)
    try:
        named: list[tuple[int, str]] = []
        for k, (i, path) in enumerate(members, start=1):
            name = f"{k:04d}.pdf"
            try:
                _place(path, os.path.join(batch_dir, name), run, ctx)
            except OSError as e:
                results[i] = _unavailable(flavour, f"Couldn't prepare the file for veraPDF: {e.strerror or e}")
                continue
            named.append((i, name))
        if not named:
            return
        timeout = single + BATCH_PER_FILE_S * (len(named) - 1)
        found, res = _run_verapdf(ctx, run, exe, flavour, [n for _, n in named], batch_dir, timeout)
        why = _why(res, timeout)
        missing = []
        for i, name in named:
            o = found.get(name.casefold())
            if o is None:
                missing.append((i, name))
            else:
                results[i] = _result(flavour, o)
        if len(named) == 1 or not missing:
            for i, _name in missing:
                results[i] = _unavailable(flavour, why)
            return
        # §5.5: a crashed batch is revalidated one file at a time.
        for i, name in missing:
            found1, res1 = _run_verapdf(ctx, run, exe, flavour, [name], batch_dir, single)
            o = found1.get(name.casefold())
            results[i] = _result(flavour, o) if o is not None else _unavailable(flavour, _why(res1, single))
    finally:
        shutil.rmtree(long_path(batch_dir), ignore_errors=True)


def _result(flavour: str, o: JobOutcome) -> CheckResult:
    return CheckResult("V-PDFA", o.state, detail=flavour, message=clip_message(o.message))


def verapdf_batch(ctx: TaskContext, run: Any, items: list[tuple[str, str]]) -> list[CheckResult]:
    """Validate (path, flavour) items. One CheckResult per item, in order (§5.5, §8)."""
    if not items:
        return []
    exe = run.tools.path("verapdf")
    java = run.tools.get("java")
    if exe is None or not java.found:
        missing = "veraPDF" if exe is None else "Java"
        return [_unavailable(f, f"{missing} is not available, so PDF/A could not be validated.", tool_missing=True)
                for _, f in items]
    results: list[CheckResult | None] = [None] * len(items)
    groups: dict[str, list[tuple[int, str]]] = {}
    for i, (path, flavour) in enumerate(items):
        groups.setdefault(flavour, []).append((i, path))
    for flavour, members in groups.items():
        _validate_group(ctx, run, exe, flavour, members, results)
    return [r if r is not None else _unavailable(items[i][1], "veraPDF produced no result.")
            for i, r in enumerate(results)]
