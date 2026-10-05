"""App factory and `baleen serve` (spec §4, §13 SEC-1/SEC-2, UI-G4).

serve():
- picks the port: --port, else BALEEN_PORT, else the first free port in 8765-8799, bound on
  127.0.0.1 only (SEC-1); the bound socket is handed to uvicorn, so there is no race;
- generates a 256-bit launch token, prints http://127.0.0.1:<port>/auth?t=<token> and opens the
  browser unless --no-browser (SEC-2);
- finalises interrupted runs (engine.recover_all, §5.6);
- runs uvicorn quietly until Quit (UI-G4) or Ctrl+C; a running job then finishes the files in
  progress and writes its report before the process exits.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import webbrowser
from collections.abc import Iterable, Mapping

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from .. import __version__, logs
from ..home import Home
from ..runner import Engine
from ..settings import SettingsStore
from ..tools import Toolset
from . import pickers
from .context import ServerContext
from .routes import not_found, routes
from .security import SecurityMiddleware
from .templating import STATIC_DIR, make_env

log = logging.getLogger("baleen.server")

PORT_RANGE = range(8765, 8800)
EXIT_FATAL = 3


# --------------------------------------------------------------------------- app


def create_app(ctx: ServerContext) -> ASGIApp:
    """The ASGI app: Starlette routes wrapped by the security middleware (outermost)."""
    ctx.templates = make_env(ctx.version)

    async def http_error(request: Request, exc: Exception) -> Response:
        code = getattr(exc, "status_code", 500)
        if code == 404 and request.method == "GET":
            return not_found(ctx, request)
        return PlainTextResponse(getattr(exc, "detail", "Error."), status_code=code)

    async def server_error(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return PlainTextResponse("Something went wrong in Baleen. Details are in data/logs/baleen.log.",
                                 status_code=500)

    app = Starlette(
        routes=[Mount("/static", StaticFiles(directory=str(STATIC_DIR), follow_symlink=False), name="static"),
                *routes()],
        exception_handlers={HTTPException: http_error, Exception: server_error},
    )
    app.state.ctx = ctx

    def forbidden() -> str:
        return ctx.templates.get_template("forbidden.html").render(title="Baleen")

    return SecurityMiddleware(app, secrets=ctx.secrets, port=ctx.port, forbidden_page=forbidden)


# --------------------------------------------------------------------------- sockets (SEC-1)


def bind_loopback(port: int) -> socket.socket:
    """A listening socket on 127.0.0.1 only. On Windows the port is exclusive, so no other
    process can bind it alongside Baleen."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        s.listen(128)
        s.set_inheritable(False)
    except OSError:
        s.close()
        raise
    return s


def choose_port(requested: int | None = None, env: Mapping[str, str] = os.environ,
                candidates: Iterable[int] = PORT_RANGE) -> tuple[socket.socket, int]:
    """--port, else BALEEN_PORT, else the first free port in 8765-8799 (SEC-1)."""
    fixed = requested
    if fixed is None and env.get("BALEEN_PORT"):
        try:
            fixed = int(env["BALEEN_PORT"])
        except ValueError as e:
            raise ValueError(f"BALEEN_PORT must be a port number, not {env['BALEEN_PORT']!r}") from e
    if fixed is not None:
        if not 1 <= fixed <= 65535:
            raise ValueError(f"Port {fixed} is out of range.")
        try:
            return bind_loopback(fixed), fixed
        except OSError as e:
            raise OSError(f"Port {fixed} on 127.0.0.1 is in use or not allowed ({e.strerror or e}).") from e
    for p in candidates:
        try:
            return bind_loopback(p), p
        except OSError:
            continue
    raise OSError("No free port between 8765 and 8799 on 127.0.0.1.")


# --------------------------------------------------------------------------- uvicorn


class BaleenServer:
    """uvicorn, quiet, on a socket we bound ourselves. Quit sets should_exit."""

    def __init__(self, app: ASGIApp, ctx: ServerContext) -> None:
        config = uvicorn.Config(
            app, log_level="warning", access_log=False, lifespan="off", server_header=False,
            date_header=False, proxy_headers=False, ws="none", http="h11", loop="asyncio",
            timeout_graceful_shutdown=3,
        )
        self.server = uvicorn.Server(config)
        ctx.quit.bind(self.request_exit)

    def request_exit(self) -> None:
        self.server.should_exit = True

    @property
    def started(self) -> bool:
        return bool(self.server.started)

    def run(self, sock: socket.socket) -> None:
        self.server.run(sockets=[sock])


def build_context(port: int, home: Home | None = None) -> ServerContext:
    home = home or Home.current()
    home.ensure()
    store = SettingsStore.open(home.settings_path)
    tools = Toolset(home)
    engine = Engine(home, store, tools)
    return ServerContext(home=home, store=store, tools=tools, engine=engine, port=port, picker=pickers.detect())


def serve(port: int | None = None, open_browser: bool = True) -> int:
    home = Home.current()
    home.ensure()
    logs.setup(home)
    try:
        sock, port = choose_port(port)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FATAL
    ctx = build_context(port, home)
    for w in ctx.store.warnings:
        print(f"warning: {w}", file=sys.stderr)
        log.warning("settings: %s", w)
    try:
        fixed = ctx.engine.recover_all()
        if fixed:
            log.warning("finalised interrupted runs: %s", ", ".join(fixed))
    except Exception:
        log.exception("recovery at start failed")
    threading.Thread(target=ctx.tools.detect, daemon=True, name="baleen-tools").start()
    app = create_app(ctx)
    url = f"http://127.0.0.1:{port}/auth?t={ctx.secrets.token}"
    print(f"Baleen {__version__} is running on http://127.0.0.1:{port}")
    print("If the browser doesn't open by itself, open this address:")
    print(url)
    print("Keep this window open. Closing it, or Quit in Baleen, stops Baleen.", flush=True)
    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    server = BaleenServer(app, ctx)
    try:
        server.run(sock)
    finally:
        try:
            if ctx.engine.running():
                print("Finishing the files in progress and writing the report…", flush=True)
                ctx.engine.cancel()
                ctx.engine.wait()
        except KeyboardInterrupt:
            pass
        sock.close()
    print("Baleen has stopped.")
    return 0
