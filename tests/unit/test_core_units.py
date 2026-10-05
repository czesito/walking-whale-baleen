"""Model, settings, report writer, paths, fsops, scan, journal."""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from baleen import settings as S
from baleen.fsops import rename_noreplace
from baleen.journal import Journal
from baleen.model import (
    REASONS,
    CheckResult,
    CheckState,
    ItemResult,
    Status,
    combine_status,
    format_checks,
    join_reasons,
    outcome_from_checks,
    parse_checks,
)
from baleen.paths import any_component_too_long, fold_key, is_within, long_path, overlap
from baleen.report import ResumeIndex, new_run_id, read_csv, write_csv
from baleen.scan import scan

# ------------------------------------------------------------------------------ model


def test_every_spec_reason_has_label_and_help() -> None:
    spec_codes = """RESUMED ATTACHMENTS_DROPPED CONTENT_MISMATCH STREAMS_DROPPED ENCODING_UNCERTAIN CHARSET_ERRORS
    MULTI_FRAME_IMAGE NOT_PDFA PDFA_INVALID PDF_ENCRYPTED SOURCE_INVALID NOT_ARCHIVAL_FORMAT VALIDATOR_MISSING
    VALIDATOR_ERROR TOOL_MISSING EML_ATTACHMENTS_BLOCKED EML_NESTING_TOO_DEEP NAME_CLASH_UNRESOLVED OUTPUT_OCCUPIED
    OUTPUT_INVALID PATH_TOO_LONG CONVERSION_ERROR PASSWORD_PROTECTED ENCODING_MISMATCH TEXT_LOSS VERIFY_FAILED
    TIMEOUT SOURCE_UNREADABLE SOURCE_CHANGED NO_WORK_SPACE UNSUPPORTED_FORMAT NO_MEDIA_STREAMS SYSTEM_FILE SYMLINK
    CANCELLED INTERRUPTED""".split()
    assert set(spec_codes) == set(REASONS)
    for r in REASONS.values():
        assert r.label and r.help


def test_severity() -> None:
    assert combine_status([]) == Status.OK
    assert combine_status(["CONTENT_MISMATCH"]) == Status.OK
    assert combine_status(["VALIDATOR_MISSING", "CONVERSION_ERROR"]) == Status.FAILED
    assert combine_status(["UNSUPPORTED_FORMAT", "CHARSET_ERRORS"]) == Status.NEEDS_REVIEW
    assert combine_status(["CANCELLED"]) == Status.SKIPPED
    assert join_reasons(["STREAMS_DROPPED", "CHARSET_ERRORS", "CHARSET_ERRORS"]) == "CHARSET_ERRORS;STREAMS_DROPPED"


def test_checks_roundtrip_and_outcomes() -> None:
    cs = [CheckResult("V-PDF-OPEN", CheckState.PASS), CheckResult("V-PDFA", CheckState.PASS, "2b")]
    assert format_checks(cs) == "V-PDF-OPEN=pass;V-PDFA=pass(2b)"
    assert [c.report_form() for c in parse_checks(format_checks(cs))] == [c.report_form() for c in cs]
    assert outcome_from_checks(cs, new_output=True) == []
    miss = [CheckResult("V-PDFA", CheckState.UNAVAILABLE, "2b", tool_missing=True)]
    assert outcome_from_checks(miss, new_output=True) == ["VALIDATOR_MISSING"]
    err = [CheckResult("V-PDFA", CheckState.UNAVAILABLE, "2b")]
    assert outcome_from_checks(err, new_output=True) == ["VALIDATOR_ERROR"]
    fail = [CheckResult("V-IMG", CheckState.FAIL), *miss]
    assert outcome_from_checks(fail, new_output=True) == ["VERIFY_FAILED"]
    assert outcome_from_checks(fail, new_output=False) == ["SOURCE_INVALID"]


# ------------------------------------------------------------------------------ settings


def test_settings_validation_and_overrides(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    res = S.load(p)
    assert res.settings == S.defaults() and res.warnings
    clean, warns = S.validate({"schema_version": 1, "workflow": {"pdfa_level": "3b", "copy_existing": "maybe"},
                               "app": {"processor_cores": 0}})
    assert clean["workflow"]["pdfa_level"] == "3b"
    assert clean["workflow"]["copy_existing"] is True and len(warns) == 2
    s2 = S.apply_overrides(S.defaults(), ["pdfa_level=1b", "copy_existing=false", "transfer_count=3"])
    assert s2["workflow"]["pdfa_level"] == "1b" and s2["workflow"]["copy_existing"] is False
    with pytest.raises(ValueError):
        S.apply_overrides(S.defaults(), ["nope=1"])
    with pytest.raises(ValueError):
        S.apply_overrides(S.defaults(), ["eml_attachments=delete"])


def test_store_reset_keeps_folders(tmp_path: Path) -> None:
    store = S.SettingsStore(tmp_path / "s.json")
    seen = []
    store.subscribe(lambda snap: seen.append(snap["app"]["processor_use"]))
    store.update({"source_dir": str(tmp_path), "pdfa_level": "1b", "processor_use": "gentle"})
    store.reset_workflow()
    w = store.workflow()
    assert w["source_dir"] == str(tmp_path) and w["pdfa_level"] == "2b"
    store.reset_resources()
    assert store.app()["processor_use"] == "balanced" and seen[0] == "gentle"
    assert S.load(tmp_path / "s.json").settings["workflow"]["source_dir"] == str(tmp_path)


# ------------------------------------------------------------------------------ report


def test_csv_format(tmp_path: Path) -> None:
    rows = [ItemResult(n=2, run_id="r", source_path='b, "quoted"\nname.doc', status="FAILED", reason="TIMEOUT"),
            ItemResult(n=1, run_id="r", source_path="訪談.doc", status="OK")]
    p = str(tmp_path / "report.csv")
    sha = write_csv(p, rows)
    raw = Path(p).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") >= 3
    assert len(sha) == 64
    text = raw.decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[0][0] == "run_id" and len(parsed[0]) == 16
    assert parsed[1][1] == "訪談.doc"  # plan order
    assert parsed[2][1] == 'b, "quoted"\nname.doc'
    back = read_csv(p)
    assert back[1].source_path == 'b, "quoted"\nname.doc' and back[1].status == "FAILED"


def test_run_id_suffix(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 14, 30, 12)
    a = new_run_id([str(tmp_path)], now=now)
    assert a == "20261005-143012"
    (tmp_path / f"report-{a}.csv").write_text("x")
    assert new_run_id([str(tmp_path)], now=now) == "20261005-143012-2"


def test_resume_index(tmp_path: Path) -> None:
    rows = [ItemResult(n=1, run_id="r", source_path="a.doc", source_sha256="s1", output_path="a.pdf",
                       output_sha256="o1", status="OK")]
    write_csv(str(tmp_path / "report-20260101-000000.csv"), rows)
    idx = ResumeIndex(str(tmp_path))
    assert idx.matches("a.doc", "s1", "a.pdf", "o1")
    assert not idx.matches("a.doc", "s2", "a.pdf", "o1")
    assert not idx.matches("a.doc", "s1", "a.pdf", "zz")


# ------------------------------------------------------------------------------ paths & fs


def test_overlap(tmp_path: Path) -> None:
    a = tmp_path / "Archive"
    (a / "sub").mkdir(parents=True)
    assert overlap(a, a / "sub") == "b_in_a"
    assert overlap(a / "sub", a) == "a_in_b"
    assert overlap(a, tmp_path / "Other") is None
    assert overlap(a, tmp_path / "Archive2") is None
    if sys.platform in ("win32", "darwin"):
        assert overlap(a, str(a).upper()) == "same"
    assert is_within(a / "sub" / "x.txt", a)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_overlap_through_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert overlap(real, link / "out") == "b_in_a"


def test_component_limits() -> None:
    assert not any_component_too_long("a/" + "x" * 255)
    assert any_component_too_long("a/" + "x" * 256)
    assert any_component_too_long("😀" * 128)  # 256 UTF-16 code units
    assert fold_key("Straße") == fold_key("STRASSE")


def test_rename_noreplace(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("old")
    with pytest.raises(FileExistsError):
        rename_noreplace(str(a), str(b))
    assert b.read_text() == "old" and a.exists()
    c = tmp_path / "c"
    rename_noreplace(str(a), str(c))
    assert c.read_text() == "new" and not a.exists()


def test_extended_length_paths(tmp_path: Path) -> None:
    deep = str(tmp_path)
    for i in range(12):
        deep = os.path.join(deep, f"{i:02d}-" + "d" * 30)
    os.makedirs(long_path(deep))
    f = os.path.join(deep, "file.txt")
    with open(long_path(f), "w") as fh:
        fh.write("ok")
    assert len(f) > 300
    entries = scan(str(tmp_path))
    assert any(e.rel.endswith("file.txt") for e in entries)


def test_scan_order_and_ignores(tmp_path: Path) -> None:
    for rel in ["b.txt", "A.txt", "Ä.txt", "sub/z.jpg", "Thumbs.db", ".DS_Store", "._x.jpg", "~$doc.docx",
                ".~lock.a.odt#", "@eaDir/thumb.jpg", "_baleen/report.csv", ".profile"]:
        p = tmp_path.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    entries = scan(str(tmp_path))
    rels = [e.rel for e in entries]
    assert rels.index("A.txt") < rels.index("b.txt")
    ign = {e.rel: e.ignore for e in entries if e.ignore}
    assert ign == {"Thumbs.db": "SYSTEM_FILE", ".DS_Store": "SYSTEM_FILE", "._x.jpg": "SYSTEM_FILE",
                   "~$doc.docx": "SYSTEM_FILE", ".~lock.a.odt#": "SYSTEM_FILE", "@eaDir": "SYSTEM_FILE",
                   "_baleen": "SYSTEM_FILE"}
    assert ".profile" in rels and "@eaDir/thumb.jpg" not in rels


def test_journal_query(tmp_path: Path) -> None:
    j = Journal.create(str(tmp_path / "j.sqlite"), {"run_id": "r"}, [])
    for n, st, sp in [(1, "OK", "a/x.jpg"), (2, "FAILED", "b/訪談.doc"), (3, "NEEDS_REVIEW", "c/y.txt"),
                      (4, "OK", "c/z.jpg")]:
        j.commit(ItemResult(n=n, run_id="r", source_path=sp, status=st))
    rows, total = j.query()
    assert total == 4 and [r.n for r in rows] == [2, 3, 1, 4]
    rows, total = j.query(statuses=["OK"])
    assert [r.n for r in rows] == [1, 4]
    rows, total = j.query(q="訪談")
    assert [r.n for r in rows] == [2]
    assert j.neighbours(3) == (2, 1)
    assert j.counts()["by_status"] == {"OK": 2, "FAILED": 1, "NEEDS_REVIEW": 1}
    assert [r.n for r in j.latest(2)] == [4, 3]
    j.close()
