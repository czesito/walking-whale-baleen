"""Golden integration tests (spec §16.3) against the real tools, plus AC-01..AC-05 per run.

Every (group, profile) pair in expected.csv converts DEST/<group> with that profile and
compares every report row. Rows whose `requires` are missing locally are skipped; with
BALEEN_REQUIRE_TOOLS=1 (CI) a missing tool is a failure instead.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import os
import sys
from functools import cache
from pathlib import Path

import pytest

from baleen import settings as S
from baleen.home import Home
from baleen.model import ARCHIVAL_TARGETS, Mode
from baleen.paths import long_path
from baleen.report import read_csv
from baleen.runner import Engine, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

from .profiles import PROFILES

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REQUIRE_ALL = os.environ.get("BALEEN_REQUIRE_TOOLS") == "1"
CONDITIONS = {"posix", "casesensitive"}

pytestmark = pytest.mark.integration


def load_expected() -> list[dict[str, str]]:
    text = (HERE / "expected.csv").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


EXPECTED = load_expected()
PAIRS = sorted({(r["group"], r["profile"]) for r in EXPECTED})


@cache
def tools_available() -> frozenset[str]:
    home = Home(Path(os.environ.get("BALEEN_HOME", str(ROOT))))
    ts = Toolset(home).detect()
    have = {k for k, t in ts.items() if t.found}
    if sys.platform != "win32":
        have.add("posix")
    if sys.platform.startswith("linux"):
        have.add("casesensitive")
    return frozenset(have)


@pytest.fixture(scope="session")
def fixtures_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tests.fixtures.make_fixtures import build

    dest = tmp_path_factory.mktemp("fixtures")
    groups = sorted({g for g, _ in PAIRS})
    status = build(dest, groups)
    (dest / "_status.txt").write_text(repr(status), encoding="utf-8")
    return dest


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    out: dict[str, tuple[int, int, str]] = {}
    base = long_path(root)
    for dp, dns, fns in os.walk(base):
        for n in dns + fns:
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, base)
            if os.path.islink(p):
                out[rel] = (0, 0, "link")
            elif os.path.isdir(p):
                out[rel + "/"] = (0, 0, "")
            else:
                st = os.stat(p)
                with open(p, "rb") as f:
                    out[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    return out


def run_profile(src: Path, out: Path, profile: str, home_dir: Path):  # noqa: ANN201
    home = Home(home_dir)
    home.ensure()
    store = SettingsStore(home.settings_path)
    st = copy.deepcopy(store.snapshot())
    for k, v in PROFILES[profile].items():
        sec, _ = S.find_field(k)
        st[sec][k] = v
    st["app"]["keep_awake"] = False
    engine = Engine(home, store, Toolset(home))
    mode = Mode.CHECK if profile == "check" else Mode.CONVERT
    return engine.start(RunSpec(mode, str(src), None if mode == Mode.CHECK else str(out), st), background=False)


def _codes(text: str) -> set[str]:
    return {c for c in text.split(";") if c}


@pytest.mark.parametrize(("group", "profile"), PAIRS, ids=[f"{g}-{p}" for g, p in PAIRS])
def test_golden(group: str, profile: str, fixtures_root: Path, tmp_path: Path) -> None:
    src = fixtures_root / group
    if not src.exists():
        status = (fixtures_root / "_status.txt").read_text(encoding="utf-8")
        if REQUIRE_ALL:
            pytest.fail(f"fixture group {group} not built: {status}")
        pytest.skip(f"fixture group {group} not built: {status}")
    rows = [r for r in EXPECTED if r["group"] == group and r["profile"] == profile]
    have = tools_available()
    runnable = []
    for r in rows:
        req = {x for x in r["requires"].split(";") if x}
        missing = req - have
        if missing - CONDITIONS and REQUIRE_ALL:
            pytest.fail(f"{r['source_path']}: required tools missing: {missing}")
        if not missing:
            runnable.append(r)
    if not runnable:
        pytest.skip("required tools missing for every row")

    before = snapshot(src)
    out = tmp_path / "out"
    job = run_profile(src, out, profile, tmp_path / "home")
    assert snapshot(src) == before, "AC-01: the source tree changed"
    all_rows = read_csv(job.report_path())
    report = {r.source_path: r for r in all_rows}
    # AC-02: every item exactly once (a dict alone would hide duplicates).
    assert len(report) == len(all_rows), "AC-02: a source_path appears more than once in the report"

    problems: list[str] = []
    for exp in runnable:
        got = report.get(exp["source_path"])
        name = exp["source_path"]
        if got is None:
            problems.append(f"{name}: no report row")
            continue
        detail = f"[{got.reason}] {got.message}"
        if got.status != exp["status"]:
            problems.append(f"{name}: status {got.status} != {exp['status']} {detail}")
        if _codes(got.reason) != _codes(exp["reason"]):
            problems.append(f"{name}: reason {got.reason!r} != {exp['reason']!r} {detail}")
        if got.output_path != exp["output_path"]:
            problems.append(f"{name}: output {got.output_path!r} != {exp['output_path']!r}")
        if exp["checks"] != "*" and got.checks != exp["checks"]:
            problems.append(f"{name}: checks {got.checks!r} != {exp['checks']!r}")
    known = {r["source_path"] for r in rows}
    extra = sorted(p for p in report if p not in known)
    if extra:
        problems.append(f"report rows missing from expected.csv: {extra}")
    assert not problems, "\n".join(problems)

    if profile != "check":
        # AC-03: every output file has a row with a matching hash; AC-04: archival formats only.
        by_out = {r.output_path: r for r in report.values() if r.output_path}
        base = long_path(out)
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d != "_baleen"]
            for f in fns:
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, base).replace("\\", "/")
                assert rel in by_out, f"AC-03: {rel} has no report row"
                with open(p, "rb") as fh:
                    assert hashlib.sha256(fh.read()).hexdigest() == by_out[rel].output_sha256, rel
                assert os.path.splitext(rel)[1] in ARCHIVAL_TARGETS, f"AC-04: {rel}"
        for r in report.values():
            if r.status in ("FAILED", "UNSUPPORTED", "IGNORED", "SKIPPED"):
                assert not r.output_path, f"AC-03: {r.source_path} has an output but status {r.status}"
            if REQUIRE_ALL:
                assert "VALIDATOR_MISSING" not in r.reason, f"AC-04: {r.source_path} VALIDATOR_MISSING"
