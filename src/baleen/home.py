"""BALEEN_HOME layout (spec §14.2): runtime/ is read-only, data/ holds all mutable state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_home() -> Path:
    env = os.environ.get("BALEEN_HOME")
    if env:
        return Path(env).resolve()
    # Source checkout: src/baleen/home.py -> repository root.
    here = Path(__file__).resolve()
    repo = here.parents[2]
    if (repo / "pyproject.toml").is_file() and (repo / "src" / "baleen").is_dir():
        return repo
    return Path.cwd().resolve()


@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def current(cls) -> Home:
        return cls(_default_home())

    # ---- read-only bundle parts
    @property
    def runtime_dir(self) -> Path:
        # BALEEN_RUNTIME (proposed DR-38): lets source checkouts and test worktrees share one
        # bundled runtime/ without copying it.
        env = os.environ.get("BALEEN_RUNTIME")
        return Path(env).resolve() if env else self.root / "runtime"

    # ---- mutable state
    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def runs_index_path(self) -> Path:
        return self.data_dir / "runs.json"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def java_dir(self) -> Path:
        return self.data_dir / "java"

    @property
    def default_work_dir(self) -> Path:
        return self.data_dir / "work"

    def lo_profile(self, k: int | None = None) -> Path:
        """Private LibreOffice profile; one per Documents-lane instance (§5.5)."""
        return self.data_dir / ("lo-profile" if k is None else f"lo-profile-{k}")

    def ensure(self) -> None:
        for p in (self.data_dir, self.reports_dir, self.logs_dir, self.tmp_dir, self.java_dir):
            p.mkdir(parents=True, exist_ok=True)

    def resolve_work_dir(self, configured: str) -> Path:
        """`work_dir` setting: relative values are relative to BALEEN_HOME (default data/work)."""
        p = Path(configured or "data/work")
        return p if p.is_absolute() else (self.root / p)
