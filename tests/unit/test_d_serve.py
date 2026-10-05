"""serve(): loopback-only binding and port choice (SEC-1, AC-12), quit (UI-G4)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from baleen.server.app import BaleenServer, bind_loopback, choose_port, create_app
from baleen.server.context import QuitControl
from tests.ui import harness

SCAN = range(8772, 8785)  # unit tests stay clear of the UI test ports (8785-8789)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_ip() -> str | None:
    """The address this machine uses on its network, if any (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: routing only
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":  # noqa: S104 - comparison only
                return ip
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


def test_bind_is_loopback_only() -> None:
    sock = bind_loopback(_free_port())
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.family == socket.AF_INET
    finally:
        sock.close()


def test_choose_port_skips_busy_ports() -> None:
    first, port = choose_port(env={}, candidates=SCAN)
    try:
        second, port2 = choose_port(env={}, candidates=SCAN)
        try:
            assert port2 != port and port2 in SCAN
        finally:
            second.close()
    finally:
        first.close()


def test_choose_port_honours_baleen_port_and_the_cli() -> None:
    p = _free_port()
    s, port = choose_port(env={"BALEEN_PORT": str(p)})
    s.close()
    assert port == p
    p2 = _free_port()
    s, port = choose_port(p2, env={"BALEEN_PORT": str(p)})  # --port wins
    s.close()
    assert port == p2


def test_choose_port_errors() -> None:
    held = bind_loopback(_free_port())
    try:
        with pytest.raises(OSError):
            choose_port(held.getsockname()[1], env={})
    finally:
        held.close()
    with pytest.raises(ValueError):
        choose_port(env={"BALEEN_PORT": "eighty"})
    with pytest.raises(ValueError):
        choose_port(70000, env={})
    with pytest.raises(OSError):
        choose_port(env={}, candidates=[])


def test_not_reachable_on_a_non_loopback_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-12: the server answers on 127.0.0.1 and refuses connections on the LAN address."""
    sock, port = choose_port(env={}, candidates=SCAN)
    ctx = harness.build_ctx(tmp_path, port, monkeypatch)
    server = BaleenServer(create_app(ctx), ctx)
    t = threading.Thread(target=server.run, args=(sock,), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(f"GET /convert HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            assert c.recv(64).startswith(b"HTTP/1.1 403")  # reachable, and locked without the cookie
        ip = lan_ip()
        if ip is None:
            pytest.skip("this machine has no non-loopback IPv4 address")
        with pytest.raises(OSError):
            socket.create_connection((ip, port), timeout=3).close()
    finally:
        server.request_exit()
        t.join(timeout=10)


def test_quit_without_a_job_exits_after_stopped_is_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = harness.build_ctx(tmp_path, 8773, monkeypatch)
    exited = threading.Event()
    ctx.quit.bind(exited.set)
    c = harness.client(ctx)
    assert c.get("/stopped").status_code == 303  # not quitting yet: back to the app
    r = c.post("/api/quit")
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/stopped"
    page = c.get("/stopped")
    assert page.status_code == 200 and "Baleen has stopped" in page.text
    assert exited.wait(5)


def test_quit_control_waits_for_a_running_job() -> None:
    calls: list[str] = []

    class Eng:
        def running(self) -> bool:
            return True

        def cancel(self) -> bool:
            calls.append("cancel")
            return True

        def wait(self, timeout: float | None = None) -> bool:
            calls.append("wait")
            return True

    q = QuitControl()
    q.bind(lambda: calls.append("exit"))
    q.request(Eng())  # type: ignore[arg-type]
    assert calls[:2] == ["cancel", "wait"] and q.ready.is_set()
