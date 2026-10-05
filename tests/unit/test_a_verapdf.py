"""V-PDFA (§8, §5.5, DR-35, R-02): veraPDF report parsing and batch behaviour.

proc.run is replaced by a fake veraPDF that answers from the files it was given, so the
batching, per-file revalidation and name mapping are tested without Java.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from baleen import proc
from baleen.model import CheckState
from baleen.verify import pdf as vpdf

from .a_support import FakeTaskContext

DATA = Path(__file__).parent / "verapdf_reports"


# --------------------------------------------------------------------------- parsing


def test_parse_mixed_report() -> None:
    got = vpdf.parse_report((DATA / "mixed.xml").read_bytes())
    assert set(got) == {"0001.pdf", "0002.pdf", "0003.pdf", "0004.pdf", "0005.pdf"}
    assert got["0001.pdf"].state == CheckState.PASS
    fail = got["0002.pdf"]
    assert fail.state == CheckState.FAIL and "PDF/A-2b" in fail.message
    assert "6.6.2.1 The Catalog dictionary" in fail.message and "6.1.3" in fail.message
    assert got["0003.pdf"].state == CheckState.FAIL and "encrypted" in got["0003.pdf"].message  # PARSE
    assert got["0004.pdf"].state == CheckState.UNAVAILABLE  # veraPDF itself failed
    assert got["0005.pdf"].state == CheckState.UNAVAILABLE and "timeout" in got["0005.pdf"].message


def test_parse_noise_namespace_and_names() -> None:
    got = vpdf.parse_report((DATA / "names.xml").read_bytes())
    assert got["訪談 紀錄.pdf"].state == CheckState.PASS  # matched by case-folded base name
    assert got["report  final.pdf"].state == CheckState.FAIL and "6.7.3" in got["report  final.pdf"].message


def test_parse_truncated_report_keeps_complete_jobs() -> None:
    got = vpdf.parse_report((DATA / "truncated.xml").read_bytes())
    assert list(got) == ["0001.pdf"] and got["0001.pdf"].state == CheckState.PASS


@pytest.mark.parametrize("data", [b"", b"Error: Could not find or load main class", b"<html>nope</html>",
                                  b"<?xml version='1.0'?><report><jobs><job><item>"])
def test_parse_garbage(data: bytes) -> None:
    assert vpdf.parse_report(data) == {}


# --------------------------------------------------------------------------- fake veraPDF


def report(results: dict[str, str]) -> bytes:
    jobs = []
    for name, state in results.items():
        if state == "parse":
            body = ('<taskException type="PARSE"><exceptionMessage>Couldn\'t parse stream</exceptionMessage>'
                    "</taskException>")
        else:
            comp = "true" if state == "pass" else "false"
            rules = ('<details><rule clause="6.2.11.4.1" status="failed"><description>Fonts shall be embedded'
                     '</description></rule></details>') if state == "fail" else ""
            body = f'<validationReport jobEndStatus="normal" profileName="PDF/A-2b validation profile" ' \
                   f'isCompliant="{comp}">{rules}</validationReport>'
        jobs.append(f"<job><item><name>C:\\some where\\{name}</name></item>{body}</job>")
    return f'<?xml version="1.0" encoding="utf-8"?><report><jobs>{"".join(jobs)}</jobs></report>'.encode()


@dataclass
class FakeVera:
    """Answers by file content: b'PASS', b'FAIL', b'PARSE' or b'HANG' (never answers)."""

    batch_mode: str = "normal"  # normal | crash | partial
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, *, timeout, env=None, cwd=None, low_priority=False, **kw):  # noqa: ANN001, ANN003, ANN204
        names = args[5:]
        assert args[1:5] == ["--format", "xml", "--flavour", args[4]]
        links = {n: os.stat(os.path.join(cwd, n)).st_nlink for n in names}
        self.calls.append({"names": names, "timeout": timeout, "env": env, "cwd": cwd, "flavour": args[4],
                           "links": links, "low": low_priority})
        answers = {}
        for n in names:
            content = Path(cwd, n).read_bytes()
            for key in ("PASS", "FAIL", "PARSE", "HANG"):
                if content.startswith(key.encode()):
                    answers[n] = key.lower()
        if len(names) > 1 and self.batch_mode == "crash":
            return proc.ProcResult(args, 1, b"", b"java.lang.OutOfMemoryError")
        if any(a == "hang" for a in answers.values()):
            if len(names) == 1:
                return proc.ProcResult(args, None, b"", b"", timed_out=True)
            answers = {n: a for n, a in answers.items() if a != "hang"}
        out = report(answers)
        if len(names) > 1 and self.batch_mode == "partial":
            out = out[: out.index(b"</job>") + 6]  # killed after the first job
        return proc.ProcResult(args, 0, out, b"")


def fake_run(tmp_path: Path, verapdf: bool = True, java: bool = True, timeout_s: int = 300):  # noqa: ANN201
    tools = SimpleNamespace(
        path=lambda k: {"verapdf": "C:/rt/verapdf/verapdf.bat" if verapdf else None,
                        "java": "C:/rt/jre/bin/java.exe" if java else None}[k],
        get=lambda k: SimpleNamespace(found=java),
    )
    work_root = tmp_path / "work" / "run1"
    work_root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(tools=tools, advanced={"verapdf_timeout_s": timeout_s}, work_root=str(work_root),
                           tools_env=lambda: {"PATH": "x", "JAVA_TOOL_OPTIONS": "-XX:-UsePerfData"})


def pdf(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content + b"\n%synthetic")
    return str(path)


def test_tool_missing(tmp_path: Path) -> None:
    for kw in ({"verapdf": False}, {"java": False}):
        res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path, **kw), [("a.pdf", "2b"), ("b.pdf", "2b")])
        assert all(r.state == CheckState.UNAVAILABLE and r.tool_missing and r.detail == "2b" for r in res)


def test_batch_maps_results_and_passes_only_ascii_names(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)
    run = fake_run(tmp_path)
    # A source read in place (Check mode) with CJK, spaces and cmd metacharacters, and a work file.
    src = pdf(tmp_path / "source 訪談" / "Q&A 100% (final)!.pdf", b"FAIL")
    work_file = pdf(Path(run.work_root) / "3" / "lo_3.pdf", b"PASS")
    other = pdf(tmp_path / "out" / "resumed.pdf", b"PARSE")
    res = vpdf.verapdf_batch(FakeTaskContext(low_priority=True), run, [(src, "2b"), (work_file, "2b"), (other, "2b")])
    assert [r.state for r in res] == [CheckState.FAIL, CheckState.PASS, CheckState.FAIL]
    assert all(r.detail == "2b" and r.check == "V-PDFA" for r in res)
    assert "6.2.11.4.1 Fonts shall be embedded" in res[0].message and "parse" in res[2].message
    call, = fake.calls
    assert call["names"] == ["0001.pdf", "0002.pdf", "0003.pdf"]  # R-02: nothing else reaches the JVM
    assert all(a.isascii() for a in call["names"])
    assert Path(call["cwd"]).parent == Path(run.work_root) and call["low"]
    assert call["timeout"] == 300 + 30 * 2
    assert call["env"]["JAVA_OPTS"] == "-Xmx1g" and call["env"]["JAVACMD"] == "C:/rt/jre/bin/java.exe"
    # Baleen's own work file is hard-linked; the source and the output are copied, never linked (P1).
    assert call["links"]["0002.pdf"] == 2 and call["links"]["0001.pdf"] == 1
    assert os.stat(src).st_nlink == 1
    assert not Path(call["cwd"]).exists()  # batch folder removed


def test_crashed_batch_is_revalidated_one_by_one(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera(batch_mode="crash")
    monkeypatch.setattr(proc, "run", fake)
    run = fake_run(tmp_path, timeout_s=120)
    items = [(pdf(tmp_path / f"f{i}.pdf", b"PASS" if i != 1 else b"FAIL"), "1b") for i in range(3)]
    res = vpdf.verapdf_batch(FakeTaskContext(), run, items)
    assert [r.state for r in res] == [CheckState.PASS, CheckState.FAIL, CheckState.PASS]
    assert [c["names"] for c in fake.calls] == [["0001.pdf", "0002.pdf", "0003.pdf"], ["0001.pdf"], ["0002.pdf"],
                                                 ["0003.pdf"]]
    assert [c["timeout"] for c in fake.calls[1:]] == [120, 120, 120]


def test_partial_report_revalidates_only_the_missing(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera(batch_mode="partial")
    monkeypatch.setattr(proc, "run", fake)
    items = [(pdf(tmp_path / f"f{i}.pdf", b"PASS"), "3b") for i in range(3)]
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), items)
    assert all(r.state == CheckState.PASS for r in res)
    assert [c["names"] for c in fake.calls] == [["0001.pdf", "0002.pdf", "0003.pdf"], ["0002.pdf"], ["0003.pdf"]]


def test_no_result_even_alone_is_validator_error(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)
    items = [(pdf(tmp_path / "ok.pdf", b"PASS"), "2b"), (pdf(tmp_path / "slow.pdf", b"HANG"), "2b")]
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), items)
    assert res[0].state == CheckState.PASS
    assert res[1].state == CheckState.UNAVAILABLE and not res[1].tool_missing
    assert "longer than 300 s" in res[1].message
    assert len(fake.calls) == 2


def test_single_file_is_not_retried(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), [(pdf(tmp_path / "s.pdf", b"HANG"), "2b")])
    assert res[0].state == CheckState.UNAVAILABLE and len(fake.calls) == 1


def test_flavours_are_grouped_and_order_kept(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)
    items = [(pdf(tmp_path / "a.pdf", b"PASS"), "2b"), (pdf(tmp_path / "b.pdf", b"FAIL"), "1a"),
             (pdf(tmp_path / "c.pdf", b"PASS"), "2b")]
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), items)
    assert [(r.state, r.detail) for r in res] == [(CheckState.PASS, "2b"), (CheckState.FAIL, "1a"),
                                                  (CheckState.PASS, "2b")]
    assert sorted(c["flavour"] for c in fake.calls) == ["1a", "2b"]


def test_unreadable_input_is_unavailable(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)
    items = [(str(tmp_path / "gone.pdf"), "2b"), (pdf(tmp_path / "ok.pdf", b"PASS"), "2b")]
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), items)
    assert res[0].state == CheckState.UNAVAILABLE and "prepare" in res[0].message
    assert res[1].state == CheckState.PASS and fake.calls[0]["names"] == ["0002.pdf"]


def test_spawn_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.ProcResult(args, None, error="FileNotFoundError: x"))
    res = vpdf.verapdf_batch(FakeTaskContext(), fake_run(tmp_path), [(pdf(tmp_path / "a.pdf", b"PASS"), "2b")])
    assert res[0].state == CheckState.UNAVAILABLE and "couldn't start" in res[0].message


def test_hardlink_fallback_copies(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fake = FakeVera()
    monkeypatch.setattr(proc, "run", fake)

    def no_links(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("hard links not supported (exFAT)")

    monkeypatch.setattr(os, "link", no_links)
    run = fake_run(tmp_path)
    res = vpdf.verapdf_batch(FakeTaskContext(), run, [(pdf(Path(run.work_root) / "1" / "x.pdf", b"PASS"), "2b")])
    assert res[0].state == CheckState.PASS and fake.calls[0]["links"]["0001.pdf"] == 1
