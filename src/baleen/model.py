"""Data model: statuses, reason codes, categories, plan items and results (spec §9, §10.1).

Everything that crosses a module boundary (planner -> runner -> converters -> journal ->
report -> UI) is defined here, so every part of Baleen speaks the same vocabulary.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class Status(enum.StrEnum):
    """The six statuses of spec §9. One report row per item, exactly one status."""

    OK = "OK"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    IGNORED = "IGNORED"
    SKIPPED = "SKIPPED"


# Default sort / tile order (UI-R3, D-14): problems first.
STATUS_ORDER: tuple[Status, ...] = (
    Status.FAILED,
    Status.NEEDS_REVIEW,
    Status.UNSUPPORTED,
    Status.OK,
    Status.IGNORED,
    Status.SKIPPED,
)
STATUS_RANK: dict[Status, int] = {s: i for i, s in enumerate(STATUS_ORDER)}

# Severity used to combine reason codes into one status (§9: FAILED > NEEDS_REVIEW > UNSUPPORTED > OK).
_SEVERITY: dict[Status, int] = {
    Status.FAILED: 4,
    Status.NEEDS_REVIEW: 3,
    Status.UNSUPPORTED: 2,
    Status.OK: 1,
}

STATUS_LABEL: dict[Status, str] = {
    Status.OK: "OK",
    Status.NEEDS_REVIEW: "Needs review",
    Status.FAILED: "Failed",
    Status.UNSUPPORTED: "Unsupported",
    Status.IGNORED: "Ignored",
    Status.SKIPPED: "Skipped",
}

# CSS/icon key used by the design's badge component (st-ok, st-review, ...).
STATUS_KEY: dict[Status, str] = {
    Status.OK: "ok",
    Status.NEEDS_REVIEW: "review",
    Status.FAILED: "failed",
    Status.UNSUPPORTED: "unsupported",
    Status.IGNORED: "ignored",
    Status.SKIPPED: "skipped",
}


class Category(enum.StrEnum):
    """Report column `category` (§10.1)."""

    IMAGE = "image"
    DOCUMENT = "document"
    TEXT = "text"
    HTML = "html"
    EMAIL = "email"
    PDF = "pdf"
    VIDEO = "video"
    AUDIO = "audio"
    OTHER = "other"


class Action(enum.StrEnum):
    """Report column `action` (§10.1)."""

    CONVERT = "convert"
    REMUX = "remux"
    COPY = "copy"
    CHECK = "check"
    NONE = "none"


class Mode(enum.StrEnum):
    CONVERT = "convert"
    CHECK = "check"


NOTE = "note"


@dataclass(frozen=True)
class ReasonInfo:
    code: str
    status: Status | str  # a Status, or NOTE for notes that never change the status
    written: str  # "yes" | "no" | "kept" | "—" (spec §9 table)
    label: str  # human label (design §10)
    help: str  # "What to do" (design §10)

    @property
    def is_note(self) -> bool:
        return self.status == NOTE


def _r(code: str, status: Status | str, written: str, label: str, help_: str) -> ReasonInfo:
    return ReasonInfo(code, status, written, label, help_)


# Spec §9 reason table, with design §10 labels and "What to do" copy.
REASONS: dict[str, ReasonInfo] = {
    r.code: r
    for r in (
        _r("RESUMED", Status.OK, "kept", "Done in an earlier run",
           "Nothing. It was checked again and is still good."),
        _r("ATTACHMENTS_DROPPED", NOTE, "yes", "Attachments listed, not saved",
           "To keep them, set E-mail attachments to Extract and run again."),
        _r("CONTENT_MISMATCH", NOTE, "—", "Extension didn't match the content",
           "Nothing. Baleen used the file's real format."),
        _r("STREAMS_DROPPED", NOTE, "—", "Subtitles or data streams not kept",
           "MP4 keeps picture and sound. Keep the original if you need the rest."),
        _r("ENCODING_UNCERTAIN", Status.NEEDS_REVIEW, "no", "Text encoding not certain",
           "Baleen couldn't tell how this text file is encoded, so it didn't guess. Choose an encoding "
           "under Options → Text encoding when unsure (the message suggests one), then run again."),
        _r("CHARSET_ERRORS", Status.NEEDS_REVIEW, "yes", "Undecodable characters",
           "Some characters show as �. The PDF was written; compare it with the original before "
           "relying on it."),
        _r("MULTI_FRAME_IMAGE", Status.NEEDS_REVIEW, "no", "Image has several frames",
           "Decide how to keep it (for example, export the frames), then add the result to the source."),
        _r("NOT_PDFA", Status.NEEDS_REVIEW, "no", "PDF is not PDF/A",
           "Convert it with a PDF/A-capable tool, or keep it as a noted exception."),
        _r("PDFA_INVALID", Status.NEEDS_REVIEW, "no", "Claims PDF/A, fails validation",
           "See veraPDF's message; re-create the PDF from its source if you can."),
        _r("PDF_ENCRYPTED", Status.NEEDS_REVIEW, "no", "PDF is password-protected",
           "Get an unprotected copy."),
        _r("SOURCE_INVALID", Status.NEEDS_REVIEW, "no", "Source file is damaged",
           "It doesn't fully decode. Look for another copy."),
        _r("NOT_ARCHIVAL_FORMAT", Status.NEEDS_REVIEW, "—", "Not archival yet",
           "Run Convert to turn it into the format named in the message."),
        _r("VALIDATOR_MISSING", Status.NEEDS_REVIEW, "yes", "Couldn't verify",
           "A checking tool is missing (see Tools). Fix it, then run Check on the output folder."),
        _r("VALIDATOR_ERROR", Status.NEEDS_REVIEW, "yes", "Checker stopped unexpectedly",
           "Run Check on the output again. If it repeats, report it with the log."),
        _r("TOOL_MISSING", Status.NEEDS_REVIEW, "no", "Converter missing",
           "The program for this format isn't available. See Tools."),
        _r("EML_ATTACHMENTS_BLOCKED", Status.NEEDS_REVIEW, "no", "Attachments blocked",
           "Switch E-mail attachments to Extract or List, then run again."),
        _r("EML_NESTING_TOO_DEEP", Status.NEEDS_REVIEW, "no", "Forwarded e-mails nested too deep",
           "Save the inner messages separately and add them to the source."),
        _r("NAME_CLASH_UNRESOLVED", Status.NEEDS_REVIEW, "no", "Name clash",
           "Two files would get the same name. Rename one in a copy of the source."),
        _r("OUTPUT_OCCUPIED", Status.NEEDS_REVIEW, "no", "Output path taken",
           "A file Baleen didn't produce is already there. Move it away or choose another output folder."),
        _r("OUTPUT_INVALID", Status.NEEDS_REVIEW, "kept", "Earlier output fails its checks",
           "Remove that file from the output folder and run again."),
        _r("PATH_TOO_LONG", Status.NEEDS_REVIEW, "no", "Path too long for Windows",
           "Shorten the folder or file name in a copy of the source."),
        _r("CONVERSION_ERROR", Status.FAILED, "no", "Converter error",
           "See the message. Open the original to check it isn't damaged."),
        _r("PASSWORD_PROTECTED", Status.FAILED, "no", "Document needs a password",
           "Get an unprotected copy."),
        _r("ENCODING_MISMATCH", Status.FAILED, "no", "Chosen encoding doesn't fit",
           "Pick another encoding under Options, then run again."),
        _r("TEXT_LOSS", Status.FAILED, "no", "Characters missing in the PDF",
           "Usually a missing font. Install a font that covers the text, then run again."),
        _r("VERIFY_FAILED", Status.FAILED, "no", "Output failed a check",
           "It wasn't saved. See the message; the source may be damaged."),
        _r("TIMEOUT", Status.FAILED, "no", "Took too long",
           "Try again. Very long recordings may need a higher limit (advanced settings)."),
        _r("SOURCE_UNREADABLE", Status.FAILED, "no", "Can't read the source",
           "The file may be damaged or locked. Check that it opens on this computer."),
        _r("SOURCE_CHANGED", Status.FAILED, "no", "Source changed during the run",
           "Run again once nothing else is writing to the folder."),
        _r("NO_WORK_SPACE", Status.FAILED, "no", "Not enough space in the work folder",
           "Free up space on the work folder's drive, or choose another work folder in Settings, "
           "then run again."),
        _r("UNSUPPORTED_FORMAT", Status.UNSUPPORTED, "no", "Format not handled",
           "Baleen v1 doesn't handle this format. It's listed so nothing goes missing."),
        _r("NO_MEDIA_STREAMS", Status.UNSUPPORTED, "no", "No audio or video inside",
           "Keep the file as it is, or remove it from the source."),
        _r("SYSTEM_FILE", Status.IGNORED, "no", "System file",
           "Nothing. Operating-system clutter, skipped on purpose."),
        _r("SYMLINK", Status.IGNORED, "no", "Link, not followed",
           "Copy the real files into the source if you need them."),
        _r("CANCELLED", Status.SKIPPED, "no", "Run was cancelled",
           "Run again to finish; completed files are skipped."),
        _r("INTERRUPTED", Status.SKIPPED, "no", "Baleen stopped before this file",
           "Run again to finish; completed files are skipped."),
    )
}

NOTE_CODES: frozenset[str] = frozenset(c for c, r in REASONS.items() if r.is_note)


def reason_status(code: str) -> Status | str:
    return REASONS[code].status


def combine_status(reasons: list[str], default: Status = Status.OK) -> Status:
    """Status implied by a list of reason codes (§9 severity; notes never change the status).

    IGNORED and SKIPPED codes are exclusive: they win if present.
    """
    best: Status = default
    for code in reasons:
        st = REASONS[code].status
        if st == NOTE:
            continue
        assert isinstance(st, Status)
        if st in (Status.IGNORED, Status.SKIPPED):
            return st
        if _SEVERITY.get(st, 0) > _SEVERITY.get(best, 0):
            best = st
    return best


def join_reasons(reasons: list[str]) -> str:
    """Report form: codes separated by ';', status codes first then notes, no duplicates."""
    seen: list[str] = []
    for c in reasons:
        if c not in seen:
            seen.append(c)
    main = [c for c in seen if c not in NOTE_CODES]
    notes = [c for c in seen if c in NOTE_CODES]
    return ";".join(main + notes)


def split_reasons(text: str) -> list[str]:
    return [c for c in (text or "").split(";") if c]


# --------------------------------------------------------------------------- checks


class CheckState(enum.StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"
    NA = "n/a"


CHECK_IDS: tuple[str, ...] = (
    "V-IMG",
    "V-PDF-OPEN",
    "V-PDFA",
    "V-TEXT",
    "V-AV-PROBE",
    "V-AV-DUR",
    "V-AV-DECODE",
    "V-HASH",
)


@dataclass
class CheckResult:
    """One verification check (§8). `detail` is shown in parentheses in the report, e.g. 2b."""

    check: str
    state: CheckState
    detail: str = ""
    message: str = ""
    # For unavailable results: True when the tool is missing (VALIDATOR_MISSING),
    # False when it crashed or produced no result (VALIDATOR_ERROR).
    tool_missing: bool = False

    def report_form(self) -> str:
        d = f"({self.detail})" if self.detail else ""
        return f"{self.check}={self.state.value}{d}"


def format_checks(checks: list[CheckResult]) -> str:
    return ";".join(c.report_form() for c in checks)


def parse_checks(text: str) -> list[CheckResult]:
    out: list[CheckResult] = []
    for part in (text or "").split(";"):
        if not part or "=" not in part:
            continue
        name, rest = part.split("=", 1)
        detail = ""
        if rest.endswith(")") and "(" in rest:
            rest, detail = rest[:-1].split("(", 1)
        try:
            state = CheckState(rest)
        except ValueError:
            continue
        out.append(CheckResult(name, state, detail))
    return out


def outcome_from_checks(checks: list[CheckResult], *, new_output: bool) -> list[str]:
    """Outcome rules of §8: return the reason codes implied by a list of checks.

    new_output=True: a new output (convert/remux) - a fail means VERIFY_FAILED (not placed).
    new_output=False: an existing archival source - a fail means SOURCE_INVALID.
    No fail but an unavailable check -> VALIDATOR_MISSING / VALIDATOR_ERROR (placed, never OK).
    """
    if any(c.state == CheckState.FAIL for c in checks):
        return ["VERIFY_FAILED" if new_output else "SOURCE_INVALID"]
    reasons: list[str] = []
    for c in checks:
        if c.state == CheckState.UNAVAILABLE:
            code = "VALIDATOR_MISSING" if c.tool_missing else "VALIDATOR_ERROR"
            if code not in reasons:
                reasons.append(code)
    return reasons


# --------------------------------------------------------------------------- scan & plan


@dataclass
class ScanEntry:
    """One entry found by the scan (§5.2). `rel` always uses '/' separators."""

    rel: str
    size: int | None
    mtime_ns: int | None
    is_dir: bool = False
    ignore: str | None = None  # "SYSTEM_FILE" | "SYMLINK" | None
    error: str | None = None  # unreadable folder: reported FAILED SOURCE_UNREADABLE

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def rel_dir(self) -> str:
        return self.rel.rsplit("/", 1)[0] if "/" in self.rel else ""


@dataclass
class Probe:
    """What a route learned by looking at the content (plan time, Files lane).

    A probe never writes anything. It may decide the item's status already (e.g.
    MULTI_FRAME_IMAGE, ENCODING_UNCERTAIN, NOT_PDFA, NO_MEDIA_STREAMS) - such items are
    final at plan time and never staged.
    """

    category: Category
    target_ext: str | None  # ".jpg", ".tif", ".pdf", ".mp4", ".m4a"; None when nothing is written
    action: Action  # planned action (convert | remux | copy | check | none)
    source_format: str = ""
    method: str = ""  # human-readable planned method; converters refine it
    reasons: list[str] = field(default_factory=list)  # status codes known now
    notes: list[str] = field(default_factory=list)  # CONTENT_MISMATCH, STREAMS_DROPPED ...
    message: str = ""
    route: str | None = None  # converter key that handles the item at run time
    data: dict[str, Any] = field(default_factory=dict)  # route-private (streams, encoding, ...)
    final: bool = False  # True: outcome decided at plan time, nothing to run


@dataclass
class PlanItem:
    """One row of the plan (§5.3, §7). Items are numbered in plan order (n, from 1).

    Attachments of e-mails are items too (source_path 'mail.eml#photo.jpg'), placed directly
    after their e-mail in plan order.
    """

    n: int
    source_path: str  # report form (§10.1 column 2)
    abs_path: str | None  # absolute path on disk for real files; None for attachments
    size: int | None
    mtime_ns: int | None
    ext: str  # lower-case source extension incl. dot, "" if none
    category: Category
    action: Action
    route: str | None
    target_ext: str | None
    out_dir: str  # output directory relative to output root, '/' separators, "" for root
    output_path: str | None  # final relative output path after the clash rule (§7.3)
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    message: str = ""
    source_format: str = ""
    method: str = ""
    parent: int | None = None  # n of the parent e-mail for attachments
    depth: int = 0  # e-mail nesting depth (0 for files on disk)
    clash_renamed: bool = False
    is_dir: bool = False
    final: bool = False  # outcome decided at plan time (IGNORED, UNSUPPORTED, plan-time problems)
    materialise: bool = True  # False: planned only for naming (attachments under block/list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def status_hint(self) -> Status | None:
        """The status already known at plan time, if any."""
        if not self.reasons:
            return None
        return combine_status(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["action"] = self.action.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlanItem:
        d = dict(d)
        d["category"] = Category(d["category"])
        d["action"] = Action(d["action"])
        return cls(**d)


@dataclass
class Plan:
    items: list[PlanItem]
    source_root: str
    output_root: str | None
    mode: Mode
    settings: dict[str, Any]

    def counts(self) -> dict[str, Any]:
        """Preview counts (UI-C4)."""
        by_cat: dict[str, int] = {}
        convert = copy = check = unsupported = ignored = problems = 0
        for it in self.items:
            if not it.materialise:
                continue
            by_cat[it.category.value] = by_cat.get(it.category.value, 0) + 1
            st = it.status_hint
            if st == Status.IGNORED:
                ignored += 1
            elif st == Status.UNSUPPORTED:
                unsupported += 1
            elif it.action in (Action.CONVERT, Action.REMUX):
                convert += 1
            elif it.action == Action.COPY:
                copy += 1
            elif it.action == Action.CHECK:
                check += 1
            if st in (Status.NEEDS_REVIEW, Status.FAILED):
                problems += 1
        return {
            "files": sum(1 for it in self.items if it.materialise and it.parent is None and not it.is_dir),
            "items": sum(1 for it in self.items if it.materialise),
            "convert": convert,
            "copy": copy,
            "check": check,
            "unsupported": unsupported,
            "ignored": ignored,
            "problems": problems,
            "by_category": by_cat,
        }


# --------------------------------------------------------------------------- results


CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "source_path",
    "source_size",
    "source_mtime",
    "source_sha256",
    "source_format",
    "category",
    "action",
    "method",
    "output_path",
    "output_size",
    "output_sha256",
    "status",
    "reason",
    "checks",
    "message",
)

MESSAGE_LIMIT = 500  # tool stderr excerpt <= 500 characters (§10.1)


def clip_message(text: str, limit: int = MESSAGE_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass
class ItemResult:
    """One report row (§10.1), keyed by plan order n."""

    n: int
    run_id: str
    source_path: str
    source_size: int | None = None
    source_mtime: str = ""  # ISO 8601 UTC
    source_sha256: str = ""
    source_format: str = ""
    category: str = Category.OTHER.value
    action: str = Action.NONE.value
    method: str = ""
    output_path: str = ""
    output_size: int | None = None
    output_sha256: str = ""
    status: str = Status.OK.value
    reason: str = ""
    checks: str = ""
    message: str = ""
    finished_at: str = ""  # not in the CSV; journal and UI only

    def csv_row(self) -> list[str]:
        def s(v: Any) -> str:
            return "" if v is None else str(v)

        return [s(getattr(self, c)) for c in CSV_COLUMNS]

    @classmethod
    def from_csv(cls, n: int, row: dict[str, str]) -> ItemResult:
        def i(v: str) -> int | None:
            return int(v) if v not in ("", None) else None

        return cls(
            n=n,
            run_id=row.get("run_id", ""),
            source_path=row.get("source_path", ""),
            source_size=i(row.get("source_size", "")),
            source_mtime=row.get("source_mtime", ""),
            source_sha256=row.get("source_sha256", ""),
            source_format=row.get("source_format", ""),
            category=row.get("category", ""),
            action=row.get("action", ""),
            method=row.get("method", ""),
            output_path=row.get("output_path", ""),
            output_size=i(row.get("output_size", "")),
            output_sha256=row.get("output_sha256", ""),
            status=row.get("status", ""),
            reason=row.get("reason", ""),
            checks=row.get("checks", ""),
            message=row.get("message", ""),
        )

    @property
    def status_enum(self) -> Status:
        return Status(self.status)


# Archival target extensions (§6).
ARCHIVAL_TARGETS: frozenset[str] = frozenset({".jpg", ".tif", ".pdf", ".mp4", ".m4a"})
