"""Shared test setup.

- Temporary folders live under %TEMP%/baleen-test/<worktree>/ (the only place tests may write
  outside the repository), so parallel worktrees never collide.
- BALEEN_HOME points at a fresh temporary folder per test session, so data/ (settings, logs,
  LibreOffice profiles) never touches the checkout.
- BALEEN_RUNTIME (proposed DR-38) selects a shared bundled runtime when set by the caller.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _base() -> Path:
    tag = hashlib.sha1(str(ROOT).encode()).hexdigest()[:8]
    name = os.environ.get("BALEEN_TEST_TAG") or f"{ROOT.name}-{tag}"
    return Path(tempfile.gettempdir()) / "baleen-test" / name


def pytest_configure(config: pytest.Config) -> None:
    if not config.option.basetemp:
        base = _base() / "pytest"
        base.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(base)


@pytest.fixture(scope="session")
def baleen_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    home = tmp_path_factory.mktemp("home")
    os.environ["BALEEN_HOME"] = str(home)
    return home


@pytest.fixture()
def home(baleen_home: Path):  # noqa: ANN201
    from baleen.home import Home

    h = Home(baleen_home)
    h.ensure()
    return h
