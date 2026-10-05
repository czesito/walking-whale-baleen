"""§5.5 budgets and dispatcher. The design prototype's resolve() is the oracle (design §12)."""

from __future__ import annotations

import itertools
import json
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from baleen.scheduler import (
    Budget,
    Lane,
    Machine,
    Scheduler,
    Task,
    describe_budget,
    image_reservation_mb,
    resolve_budget,
)

PROTO = Path(__file__).resolve().parents[2] / "docs" / "prototype" / "baleen-prototype.html"


def test_examples_from_spec() -> None:
    m = Machine(cores=8, ram_gb=16, network=False)
    assert resolve_budget({"processor_use": "gentle"}, m).B == 2
    assert resolve_budget({"processor_use": "balanced"}, m).B == 4
    assert resolve_budget({"processor_use": "maximum"}, m).B == 7
    assert resolve_budget({}, m).M == 4  # 16 GB -> 4 GB
    assert resolve_budget({}, m).T == 8
    assert resolve_budget({}, Machine(8, 16, True)).T == 4
    b = resolve_budget({"processor_use": "custom", "processor_cores": 99}, m)
    assert b.B == 8  # never more than cores
    b = resolve_budget({"processor_use": "maximum"}, Machine(16, 32, False))
    assert (b.B, b.F, b.threads) == (15, 2, 7)


def _proto_resolve_source() -> str:
    html = PROTO.read_text(encoding="utf-8")
    m = re.search(r"function resolve\(r\)\{.*?\n\}", html, re.S)
    assert m, "resolve() not found in the prototype"
    return m.group(0)


CASES = list(itertools.product(
    [1, 2, 3, 4, 6, 8, 12, 16, 28, 64],  # cores
    [4, 8, 16, 32, 80],  # RAM GB
    [False, True],  # network
    ["gentle", "balanced", "maximum", "custom"],
    [1, 3, 100],  # custom cores
    ["auto", "custom"],
    [1, 6, 1000],  # custom memory
    ["auto", "custom"],
    [1, 5, 99],  # custom transfers
))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available for the JS oracle")
def test_resolve_matches_prototype_oracle() -> None:
    rnd = random.Random(1234)
    cases = rnd.sample(CASES, 3000)
    js = _proto_resolve_source()
    payload = [
        {"machine": {"cores": c, "ramGb": ram, "network": net},
         "r": {"processor": pu, "cores": cc, "memory": ml, "memGb": mg, "transfers": tl, "transfersN": tn}}
        for c, ram, net, pu, cc, ml, mg, tl, tn in cases
    ]
    script = (
        "var MACHINE;\n" + js + "\n"
        "var input=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "var out=input.map(function(x){MACHINE=x.machine;return resolve(x.r);});"
        "process.stdout.write(JSON.stringify(out));"
    )
    res = subprocess.run(["node", "-e", script], input=json.dumps(payload), capture_output=True, text=True,
                         timeout=60, check=True)
    oracle = json.loads(res.stdout)
    for case, exp in zip(cases, oracle, strict=True):
        c, ram, net, pu, cc, ml, mg, tl, tn = case
        prefs = {"processor_use": pu, "processor_cores": cc, "memory_limit": ml, "memory_gb": mg,
                 "transfer_slots": tl, "transfer_count": tn}
        got = resolve_budget(prefs, Machine(c, ram, net))
        assert (got.B, got.M, got.T, got.K, got.F, got.threads) == (
            exp["B"], exp["M"], exp["T"], exp["K"], exp["F"], exp["threads"]), case


def test_describe_budget_plain_words() -> None:
    m = Machine(8, 16, True)
    d = describe_budget(resolve_budget({}, m), m)
    assert d["using"] == "Using 4 of 8 cores · up to 4 GB memory · 4 transfers"
    for word in ("token", "thread", "worker"):
        assert word not in d["summary"].lower()


def test_image_reservation() -> None:
    assert image_reservation_mb(4000, 3000, 3) == 0  # 12 MP
    assert image_reservation_mb(10000, 5000, 3) == pytest.approx(2 * 10000 * 5000 * 3 / (1 << 20), abs=1)


# ------------------------------------------------------------------------------- dispatcher


class Probe:
    def __init__(self, sched_budget: Budget) -> None:
        self.lock = threading.Lock()
        self.tokens = 0
        self.mem = 0
        self.max_tokens = 0
        self.max_mem = 0
        self.order: list[tuple[str, int]] = []
        self.budget = sched_budget


def _task(probe: Probe, lane: Lane, n: int, dur: float = 0.01, **kw) -> Task:
    def fn(ctx) -> int:  # noqa: ANN001
        with probe.lock:
            probe.tokens += ctx.tokens
            probe.mem += ctx.memory_mb
            probe.max_tokens = max(probe.max_tokens, probe.tokens)
            probe.max_mem = max(probe.max_mem, probe.mem)
            probe.order.append((lane.value, n))
        time.sleep(dur)
        with probe.lock:
            probe.tokens -= ctx.tokens
            probe.mem -= ctx.memory_mb
        return n

    return Task(lane=lane, order=(n,), fn=fn, item=n, **kw)


def test_budgets_never_exceeded() -> None:
    b = resolve_budget({"processor_use": "custom", "processor_cores": 4, "memory_limit": "custom",
                        "memory_gb": 2}, Machine(8, 16, False))
    probe = Probe(b)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    futs = []
    for n in range(60):
        lane = [Lane.FILES, Lane.DOCUMENTS, Lane.MEDIA][n % 3]
        futs.append(s.submit(_task(probe, lane, n, dur=0.005)))
    for f in futs:
        f.result(timeout=30)
    s.close()
    assert probe.max_tokens <= b.B
    assert probe.max_mem <= b.M * 1024
    snap = s.snapshot()
    assert snap.peak_tokens <= b.B and snap.peak_memory_mb <= b.M * 1024


def test_plan_order_within_lane() -> None:
    b = Budget(B=1, M=4, T=1, K=1, F=1, threads=1)
    probe = Probe(b)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    tasks = [_task(probe, Lane.FILES, n) for n in (5, 3, 9, 1, 7)]
    gate = threading.Event()

    def blocker(ctx) -> None:  # noqa: ANN001
        gate.wait(5)

    s.submit(Task(lane=Lane.FILES, order=(0,), fn=blocker))
    time.sleep(0.05)
    for t in tasks:
        s.submit(t)
    gate.set()
    for t in tasks:
        t.future.result(timeout=10)
    s.close()
    assert [n for _, n in probe.order] == [1, 3, 5, 7, 9]


def test_oversize_task_runs_alone() -> None:
    b = Budget(B=2, M=1, T=2, K=1, F=1, threads=2)
    probe = Probe(b)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    big = _task(probe, Lane.FILES, 0, dur=0.05, memory_mb=4096)
    small = [_task(probe, Lane.FILES, n, dur=0.02) for n in range(1, 5)]
    s.submit(big)
    for t in small:
        s.submit(t)
    for t in [big, *small]:
        t.future.result(timeout=10)
    s.close()
    assert probe.max_mem == 4096  # the big one ran, and nothing ran beside it
    assert probe.order[0] == ("files", 0)


def test_live_budget_change_applies_at_next_dispatch() -> None:
    state = {"b": Budget(B=1, M=4, T=1, K=1, F=1, threads=1)}
    probe = Probe(state["b"])
    s = Scheduler(lambda: state["b"], low_priority=lambda: False)
    tasks = [_task(probe, Lane.FILES, n, dur=0.05) for n in range(12)]
    for t in tasks:
        s.submit(t)
    time.sleep(0.12)
    assert probe.max_tokens == 1
    state["b"] = Budget(B=4, M=4, T=1, K=1, F=1, threads=4)
    s.wake()
    for t in tasks:
        t.future.result(timeout=10)
    s.close()
    assert probe.max_tokens == 4


def test_batches_group_by_key_and_respect_max() -> None:
    b = Budget(B=4, M=8, T=4, K=2, F=1, threads=4)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    seen: list[list[int]] = []
    gate = threading.Event()

    def blocker(ctx) -> None:  # noqa: ANN001
        gate.wait(5)

    def batch(ctx, payloads):  # noqa: ANN001, ANN202
        seen.append(list(payloads))
        return payloads

    # Hold the Documents lane (K=2) so the queue fills before batches form.
    s.submit(Task(lane=Lane.DOCUMENTS, order=(-2,), fn=blocker))
    s.submit(Task(lane=Lane.DOCUMENTS, order=(-1,), fn=blocker))
    time.sleep(0.05)
    tasks = [Task(lane=Lane.DOCUMENTS, order=(n,), batch_fn=batch, payload=n,
                  batch_key="A" if n % 3 else "B") for n in range(20)]
    for t in tasks:
        s.submit(t)
    gate.set()
    assert [t.future.result(timeout=10) for t in tasks] == list(range(20))
    s.close()
    assert all(len(g) <= 8 for g in seen)
    for g in seen:
        assert len({"A" if n % 3 else "B" for n in g}) == 1
    assert sorted(n for g in seen for n in g) == list(range(20))


def test_pdfa_batch_waits_then_closes_when_idle() -> None:
    b = Budget(B=2, M=4, T=2, K=1, F=1, threads=2)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    seen: list[list[int]] = []

    def batch(ctx, payloads):  # noqa: ANN001, ANN202
        seen.append(list(payloads))
        return payloads

    t0 = time.monotonic()
    t = Task(lane=Lane.PDFA, order=(1,), batch_fn=batch, payload=1, batch_key="2b")
    s.submit(t)
    t.future.result(timeout=5)
    s.close()
    assert time.monotonic() - t0 < 3  # nothing else left: closes immediately, not after 10 s
    assert seen == [[1]]


def test_transfers_capped() -> None:
    b = Budget(B=8, M=8, T=2, K=1, F=1, threads=8)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    lock = threading.Lock()
    cur = {"n": 0, "max": 0}

    def fn(ctx) -> None:  # noqa: ANN001
        with ctx.transfer():
            with lock:
                cur["n"] += 1
                cur["max"] = max(cur["max"], cur["n"])
            time.sleep(0.02)
            with lock:
                cur["n"] -= 1

    tasks = [Task(lane=Lane.FILES, order=(n,), fn=fn) for n in range(16)]
    for t in tasks:
        s.submit(t)
    for t in tasks:
        t.future.result(timeout=10)
    s.close()
    assert cur["max"] == 2


def test_cancel_queued() -> None:
    b = Budget(B=1, M=4, T=1, K=1, F=1, threads=1)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    gate, started = threading.Event(), threading.Event()

    def hold(ctx) -> None:  # noqa: ANN001
        started.set()
        gate.wait(5)

    s.submit(Task(lane=Lane.FILES, order=(0,), fn=hold, kind="stage"))
    assert started.wait(5)
    queued = [Task(lane=Lane.FILES, order=(n,), fn=lambda ctx: None, kind="stage") for n in range(1, 5)]
    for t in queued:
        s.submit(t)
    removed = s.cancel_queued(lambda t: t.kind == "stage")
    gate.set()
    s.close()
    assert len(removed) == 4 and all(t.future.cancelled() for t in queued)


def test_multi_token_task_is_not_starved_by_file_tasks() -> None:
    """A Media task needing all B tokens still starts while later 1-token tasks keep arriving."""
    b = Budget(B=4, M=8, T=4, K=1, F=1, threads=4)
    s = Scheduler(lambda: b, low_priority=lambda: False)
    started: dict[str, float] = {}
    t0 = time.monotonic()

    def small_for(d: float):  # noqa: ANN202
        def small(ctx) -> None:  # noqa: ANN001
            time.sleep(d)
        return small

    def media(ctx) -> None:  # noqa: ANN001
        started["media"] = time.monotonic() - t0
        assert ctx.tokens == 4

    for n in range(4):  # staggered, so the four tokens are never free at the same moment
        s.submit(Task(lane=Lane.FILES, order=(n,), fn=small_for(0.1 + 0.07 * n)))
    m = Task(lane=Lane.MEDIA, order=(5,), fn=media)
    s.submit(m)
    # A long stream of later file tasks (orders 6..) keeps arriving.
    stop = time.monotonic() + 8
    n = 6
    while time.monotonic() < stop and not m.future.done():
        s.submit(Task(lane=Lane.FILES, order=(n,), fn=small_for(0.1 + 0.07 * (n % 4))))
        n += 1
        time.sleep(0.01)
    m.future.result(timeout=15)
    s.cancel_queued()
    s.close()
    assert 1.5 < started["media"] < 5.0  # waited for the aging threshold, then got all four tokens
