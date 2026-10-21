"""Proxy plumbing: line parsing, the stale-global fix, and the forwarder ring.

The forwarder is a local TCP relay on 127.0.0.1:<port>. mitmproxy (or the
emulator directly) points at it, and it dials the currently-assigned upstream
proxy. That indirection is what makes rotation work: the listener port never
changes, so nothing downstream has to be reconfigured when the upstream moves.

Auth: the client speaks CONNECT/GET to the upstream, so we inject
Proxy-Authorization into the first header block we see. mitmproxy sends its
CONNECT in one write, which is the case this handles.
"""
from __future__ import annotations

import base64
import socket
import threading
import time
from pathlib import Path

from core.log import log


def parse(line: str):
    """host:port or host:port:user:pass -> (host, port, user, password)."""
    parts = line.strip().split(":")
    if len(parts) == 2:
        return parts[0], parts[1], None, None
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    raise ValueError(f"bad proxy line: {line!r}")


def load_proxies(src) -> list[str]:
    """Accept a path, or a Config (in which case no_proxy is honoured)."""
    if hasattr(src, "net"):
        if getattr(src.net, "no_proxy", False):
            return []
        path = Path(src.net.proxy_file)
    else:
        path = Path(src)
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def load(cfg) -> list[str]:
    """v1-compatible alias."""
    return load_proxies(cfg)


def apply(dev, proxy: str | None, log=print) -> None:
    """Write/clear the guest's global http_proxy. Clearing REQUIRES delete."""
    if not proxy:
        log("   proxy -> direct (deleting the global setting)")
        dev.shell("settings delete global http_proxy", check=False)
        dev.shell("settings delete global global_http_proxy_host", check=False)
        dev.shell("settings delete global global_http_proxy_port", check=False)
        return
    host, port, user, password = parse(proxy)
    log(f"   proxy -> {host}:{port}")
    dev.shell(f"settings put global http_proxy {host}:{port}", check=False)
    if user:
        dev.shell(f"settings put global global_http_proxy_username {user}", check=False)
        dev.shell(f"settings put global global_http_proxy_password {password}", check=False)


class Forwarders:
    """One local relay per instance port, each rotating over the proxy pool."""

    def __init__(self, proxies: list[str], mode: str = "rotate",
                 dead_cooldown: float = 60.0, handshake_timeout: float = 10.0):
        self._proxies = list(proxies)
        self._mode = mode if mode in ("rotate", "sticky", "fixed") else "rotate"
        self._dead_cooldown = float(dead_cooldown)
        self._handshake_timeout = float(handshake_timeout)
        self._dead: dict[str, float] = {}
        self._rr = 0
        self._sticky: dict[int, str] = {}
        self._listeners: dict[int, socket.socket] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- pool ------------------------------------------------------------
    def _live(self) -> list[str]:
        now = time.time()
        with self._lock:
            return [p for p in self._proxies if self._dead.get(p, 0.0) <= now]

    def pick(self, port: int) -> str | None:
        live = self._live() or self._proxies
        if not live:
            return None
        if self._mode == "fixed":
            return live[0]
        with self._lock:
            if self._mode == "sticky":
                if port not in self._sticky:
                    self._sticky[port] = live[self._rr % len(live)]
                    self._rr += 1
                return self._sticky[port]
            chosen = live[self._rr % len(live)]
            self._rr += 1
            return chosen

    def report_dead(self, proxy: str) -> None:
        with self._lock:
            self._dead[proxy] = time.time() + self._dead_cooldown
        log(f"proxy benched {self._dead_cooldown:.0f}s: {proxy}", "warn")

    # ---- listeners -------------------------------------------------------
    def ensure(self, port: int) -> None:
        if port in self._listeners:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(64)
        self._listeners[port] = srv
        threading.Thread(target=self._accept, args=(srv, port), daemon=True).start()
        log(f"forwarder 127.0.0.1:{port} -> {self._mode} over "
            f"{len(self._proxies)} prox{'y' if len(self._proxies) == 1 else 'ies'}")

    def _accept(self, srv: socket.socket, port: int) -> None:
        while not self._stop.is_set():
            try:
                srv.settimeout(0.5)
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client, port),
                             daemon=True).start()

    def _serve(self, client: socket.socket, port: int) -> None:
        proxy = self.pick(port)
        if not proxy:
            client.close()
            return
        try:
            host, cport, user, password = parse(proxy)
        except ValueError:
            self.report_dead(proxy)
            client.close()
            return
        try:
            up = socket.create_connection((host, int(cport)),
                                          timeout=self._handshake_timeout)
        except OSError as exc:
            self.report_dead(proxy)
            log(f"forwarder {port}: {proxy} unreachable ({exc})", "warn")
            client.close()
            return
        client.settimeout(None)
        up.settimeout(None)
        auth = None
        if user:
            raw = f"{user}:{password}".encode()
            auth = b"Proxy-Authorization: Basic " + base64.b64encode(raw) + b"\r\n"
        threading.Thread(target=self._pipe, args=(client, up, auth), daemon=True).start()
        threading.Thread(target=self._pipe, args=(up, client, None), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket, inject: bytes | None) -> None:
        first = True
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if (first and inject and b"\r\n\r\n" in data
                        and b"Proxy-Authorization" not in data):
                    head, _, rest = data.partition(b"\r\n\r\n")
                    data = head + b"\r\n" + inject + b"\r\n" + rest
                first = False
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def stop_all(self) -> None:
        self._stop.set()
        for srv in self._listeners.values():
            try:
                srv.close()
            except OSError:
                pass
        self._listeners.clear()