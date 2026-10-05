"""A real Baleen server, in-process, for browser tests (ports 8785-8789).

The server is the production app (security middleware, routes, templates, uvicorn on a socket
bound to 127.0.0.1), with fake converters (fake_routes), a fake tool set and the prototype's
simulated machine (8 cores, 16 GB, folders on a network drive), so run states are
deterministic and screenshots line up with the design prototype.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baleen import scheduler
from baleen.home import Home
from baleen.runner import Engine
from baleen.scan import scan
from baleen.scheduler import Machine
from baleen.server import pickers
from baleen.server.app import BaleenServer, bind_loopback, create_app
from baleen.server.context import ServerContext
from baleen.settings import SettingsStore
from baleen.tools import SPECS, TOOL_KEYS, ToolInfo, Toolset

from . import fake_routes

UI_PORTS = (8785, 8786, 8787, 8788, 8789)

TOOL_FAKES = {
    "libreoffice": ("26.8.1", "runtime/LibreOffice.app/Contents/MacOS/soffice"),
    "ffmpeg": ("7.1.1", "runtime/ffmpeg/ffmpeg"),
    "ffprobe": ("7.1.1", "runtime/ffmpeg/ffprobe"),
    "java": ("Temurin 21.0.8", "runtime/jre/bin/java"),
    "verapdf": ("1.28.2", "runtime/verapdf/verapdf"),
}


class FakeToolset(Toolset):
    """All five tools 'bundled', or some missing; Check again re-reads `missing`."""

    def __init__(self, home: Home, missing: set[str] | None = None) -> None:
        super().__init__(home, env={})
        self.missing_set: set[str] = set(missing or ())

    def detect(self, force: bool = False) -> dict[str, ToolInfo]:
        out: dict[str, ToolInfo] = {}
        for k in TOOL_KEYS:
            spec = SPECS[k]
            if k in self.missing_set:
                out[k] = ToolInfo(k, spec.name, spec.role, None, "", "missing", "Not found")
            else:
                ver, path = TOOL_FAKES[k]
                out[k] = ToolInfo(k, spec.name, spec.role, path, ver, "bundled", "", None)
        return out

    def env(self) -> dict[str, str]:
        return {}


@dataclass
class Harness:
    ctx: ServerContext
    server: BaleenServer
    thread: threading.Thread
    port: int
    tools: FakeToolset
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth_url(self) -> str:
        return f"{self.base}/auth?t={self.ctx.secrets.token}"

    def stop(self) -> None:
        self.server.request_exit()
        self.thread.join(timeout=15)

    def wait_held(self, n: int, timeout: float = 30.0) -> None:
        """Wait until n items are blocked on the gate (and the run is otherwise idle)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(fake_routes.HELD) >= n:
                time.sleep(0.5)
                return
            time.sleep(0.05)
        raise TimeoutError(f"only {len(fake_routes.HELD)} items held")

    def wait_idle(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.ctx.engine.running():
                return
            time.sleep(0.05)
        raise TimeoutError("job still running")


class FakePicker:
    """Stands in for the native folder dialog (which cannot be automated, §12.9)."""

    path: str | None = None
    delay: float = 0.0
    calls: int = 0

    @classmethod
    def pick_folder(cls, kind: str, initial: str | None = None, **kw: Any) -> pickers.PickResult:
        cls.calls += 1
        time.sleep(cls.delay)
        return pickers.PickResult(cls.path, cls.path is None)


def build_ctx(tmp: Path, port: int, mp: Any, *, missing: set[str] | None = None, cores: int = 8, ram: int = 16,
              network: bool = True, picker: str | None = "test") -> ServerContext:
    """A server context with fake converters, tools, machine and folder dialog (no uvicorn)."""
    fake_routes.install(mp)
    FakePicker.path, FakePicker.delay, FakePicker.calls = None, 0.0, 0
    mp.setattr(pickers, "pick_folder", FakePicker.pick_folder)
    machine = Machine(cores, ram, network)
    mp.setattr(scheduler.Machine, "detect", classmethod(lambda cls, paths=(): machine))
    home = Home(tmp / "home")
    home.ensure()
    store = SettingsStore.open(home.settings_path)
    tools = FakeToolset(home, missing)
    engine = Engine(home, store, tools)
    engine.recover_all()
    return ServerContext(home=home, store=store, tools=tools, engine=engine, port=port, picker=picker)


def start(tmp: Path, port: int, mp: Any, **kw: Any) -> Harness:
    """Start a real server on 127.0.0.1:<port>; `mp` is a pytest MonkeyPatch (fakes undone with it)."""
    ctx = build_ctx(tmp, port, mp, **kw)
    tools = ctx.tools
    assert isinstance(tools, FakeToolset)
    sock = bind_loopback(port)
    server = BaleenServer(create_app(ctx), ctx)
    t = threading.Thread(target=server.run, args=(sock,), daemon=True, name=f"baleen-ui-{port}")
    t.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    return Harness(ctx, server, t, port, tools)


def client(ctx: ServerContext, *, cookie: bool = True, origin: bool = True, hx: bool = True) -> Any:
    """Starlette TestClient as the browser tab Baleen opened: right Host, loopback client, session
    cookie and same-origin Origin header (each can be dropped to test SEC-3 / SEC-5); hx sends
    HX-Request like htmx does."""
    from starlette.testclient import TestClient

    headers = {"origin": ctx.origin} if origin else {}
    if hx:
        headers["hx-request"] = "true"
    c = TestClient(create_app(ctx), base_url=ctx.origin, client=("127.0.0.1", 50123), headers=headers,
                   follow_redirects=False)
    if cookie:
        c.cookies.set("baleen_session", ctx.secrets.session)
    return c


# --------------------------------------------------------------------------- source trees


def write(root: Path, rel: str, data: bytes) -> Path:
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# The design prototype's sample rows (ROWS in baleen-prototype.html), with outcome markers.
PROTOTYPE_ROWS: tuple[tuple[str, bytes], ...] = (
    ("2003-12-28 Night market/clip0026.AVI", b"BAD video"),
    ("2004-07 Poetry evening/notes.txt", b"BIG5 text"),
    ("2004-05-30 Band showcase/訪談.eml", b"CHARSET mail"),
    ("2002-02 Folk session/flyer.gif", b"MULTI gif"),
    ("00 Series overview/programme-2004.pdf", b"%PDF NOTPDFA"),
    ("2003-03 Duo guitars/set list.pdf", b"%PDF NOTPDFA"),
    ("2004-02 Correspondence/meeting notes.msg", b"msg"),
    ("2004-01 Mixed night/backup.zip", b"zip"),
    ("2001-09 Cello evening/DP2_0001.JPG", b"jpeg"),
    ("2001-09 Cello evening/DP2_0002.JPG", b"jpeg"),
    ("2003-11-01 Jazz night/DSCF0128.AVI", b"avi"),
    ("2003-11-01 Jazz night/IMG_0007.JPG", b"jpeg"),
    ("2003-12-06 Sound art/score.doc", b"doc"),
    ("2003-12-06 Sound art/scan.bmp", b"bmp"),
    ("2004-01 Mixed night/dj/MOV00265.MPG", b"mpg"),
    ("2004-01 Mixed night/talk/track 1.wma", b"wma"),
    ("2004-04-11 Band showcase/訪談.doc", b"doc"),
    ("2004-04-11 Band showcase/訪談.eml", b"mail"),
    ("2004-05-02 Vinyl set/DJ notes.rtf", b"rtf"),
    ("2004-05-08 Guqin/0508 profile.eml", b"mail"),
    ("2004-05-08 Guqin/portrait.tif", b"tiff"),
    ("2005-01-15 New year session/MVI_2211.MOV", b"mov"),
    ("2004-01 Mixed night/Thumbs.db", b"x"),
    ("2003-11-01 Jazz night/Thumbs.db", b"x"),
    (".DS_Store", b"x"),
)
EVENTS = ("2001-09 Cello evening", "2003-11-01 Jazz night", "2003-12-06 Sound art", "2004-04-11 Band showcase",
          "2005-01-15 New year session")


def make_archive(root: Path, total: int = 1670, converts: int = 48) -> list[str]:
    """The prototype's 1,670-file archive: its sample rows plus JPEG copies and BMP conversions.
    Returns the files in plan order (the scan order of §5.2)."""
    for rel, data in PROTOTYPE_ROWS:
        write(root, rel, data)
    filler = total - len(PROTOTYPE_ROWS)
    for i in range(filler):
        ev = EVENTS[i % len(EVENTS)]
        if i < converts:
            write(root, f"{ev}/scan_{i + 1:04d}.bmp", b"bmp")
        else:
            write(root, f"{ev}/IMG_{i + 11:04d}.JPG", b"jpeg")
    return [e.rel for e in scan(str(root)) if not e.is_dir]


def hold_files(root: Path, order: list[str], start: int, count: int) -> list[str]:
    """Mark `count` image files from plan position `start` (0-based) to wait on the gate."""
    held = []
    for rel in order[start:]:
        if rel.lower().endswith((".jpg", ".bmp")) and len(held) < count:
            p = root.joinpath(*rel.split("/"))
            p.write_bytes(b"HOLD" + p.read_bytes())
            held.append(rel)
    return held


def make_check_tree(root: Path) -> None:
    """The prototype's Check sample (CHECK_ROWS)."""
    for rel, data in (
        ("2002 Catalogue/catalogue-final.doc", b"doc"),
        ("2002 Catalogue/cover.png", b"png"),
        ("2004 Zine/issue-3.pdf", b"%PDF BADPDFA"),
        ("2004 Zine/issue-1.pdf", b"%PDF"),
        ("2004 Zine/issue-2.pdf", b"%PDF"),
        ("2002 Catalogue/spreads/p01.tif", b"tiff"),
        ("2002 Catalogue/spreads/p02.tif", b"tiff"),
    ):
        write(root, rel, data)


def make_batch(root: Path, n: int = 24, hold_from: int = 10) -> None:
    """The prototype's cancelled run: 'test batch' with 24 BMP scans; scans from hold_from wait."""
    for i in range(1, n + 1):
        write(root, f"scan_{i:02d}.bmp", (b"HOLD" if i >= hold_from else b"") + b"bmp")


def ui_shots_dir() -> Path | None:
    d = os.environ.get("BALEEN_UI_SHOTS")
    if not d:
        return None
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p
