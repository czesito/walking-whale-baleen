"""Route registry under concurrent first use (plan-time probes run in parallel threads)."""

from __future__ import annotations

import importlib
import sys
import threading
import time

from baleen.convert import base
from baleen.convert.formats import MissingRoute


def test_registry_complete_when_first_used_from_many_threads(monkeypatch) -> None:  # noqa: ANN001
    # Start from a cold registry and force the media module to be imported again, slowly, so
    # every thread asks for a route while the first one is still importing.
    monkeypatch.setattr(base, "_registry", {})
    monkeypatch.setattr(base, "_loaded", False)
    monkeypatch.delitem(sys.modules, "baleen.convert.media", raising=False)
    real_import = importlib.import_module

    def slow_import(name: str, package: str | None = None):  # noqa: ANN202
        time.sleep(0.05)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", slow_import)
    barrier = threading.Barrier(8)
    got: list[base.Route] = []

    def worker() -> None:
        barrier.wait()
        got.append(base.get_route("media"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(got) == 8
    assert not any(isinstance(r, MissingRoute) for r in got), "a thread saw a half-filled registry"
    assert {r.key for r in got} == {"media"}
