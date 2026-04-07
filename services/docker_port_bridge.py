from __future__ import annotations

import os
import selectors
import signal
import socket
import socketserver
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeSpec:
    name: str
    listen_host: str
    listen_port: int
    upstream_host_env: str
    upstream_port_env: str
    default_upstream_host: str
    default_upstream_port: int


BRIDGE_SPECS = (
    BridgeSpec(
        name="cliproxyapi",
        listen_host="0.0.0.0",
        listen_port=8317,
        upstream_host_env="CLIPROXYAPI_UPSTREAM_HOST",
        upstream_port_env="CLIPROXYAPI_UPSTREAM_PORT",
        default_upstream_host="host.docker.internal",
        default_upstream_port=8317,
    ),
    BridgeSpec(
        name="grok2api",
        listen_host="0.0.0.0",
        listen_port=8011,
        upstream_host_env="GROK2API_UPSTREAM_HOST",
        upstream_port_env="GROK2API_UPSTREAM_PORT",
        default_upstream_host="host.docker.internal",
        default_upstream_port=8011,
    ),
)


def _log(message: str) -> None:
    print(f"[port-bridge] {message}", flush=True)


def _env_host(name: str, default: str) -> str:
    return str(os.getenv(name, default) or "").strip()


def _env_port(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default)) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return default


def bridge_target(spec: BridgeSpec) -> tuple[str, int] | None:
    host = _env_host(spec.upstream_host_env, spec.default_upstream_host)
    port = _env_port(spec.upstream_port_env, spec.default_upstream_port)
    if not host or port <= 0:
        return None
    return host, port


def can_connect(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def relay_bidirectional(left: socket.socket, right: socket.socket) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        while True:
            events = selector.select(timeout=1.0)
            if not events:
                continue
            for key, _ in events:
                current: socket.socket = key.fileobj
                peer: socket.socket = key.data
                try:
                    data = current.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                peer.sendall(data)


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: BridgeServer = self.server  # type: ignore[assignment]
        try:
            upstream = socket.create_connection((server.upstream_host, server.upstream_port), timeout=5)
        except OSError as exc:
            _log(
                f"{server.bridge_name}: connect upstream failed "
                f"{server.upstream_host}:{server.upstream_port} error={exc}"
            )
            return

        self.request.settimeout(60)
        upstream.settimeout(60)
        try:
            relay_bidirectional(self.request, upstream)
        finally:
            upstream.close()


class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        bind_address: tuple[str, int],
        *,
        bridge_name: str,
        upstream_host: str,
        upstream_port: int,
    ) -> None:
        self.bridge_name = bridge_name
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        super().__init__(bind_address, BridgeHandler)


def start_bridge_server(
    bridge_name: str,
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
) -> BridgeServer:
    server = BridgeServer(
        (listen_host, listen_port),
        bridge_name=bridge_name,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"bridge-{bridge_name}")
    thread.start()
    return server


def ensure_bridge(spec: BridgeSpec) -> BridgeServer | None:
    target = bridge_target(spec)
    if not target:
        _log(f"{spec.name}: disabled")
        return None

    if can_connect("127.0.0.1", spec.listen_port):
        _log(f"{spec.name}: skip local listener already active on {spec.listen_port}")
        return None

    upstream_host, upstream_port = target
    server = start_bridge_server(
        spec.name,
        spec.listen_host,
        spec.listen_port,
        upstream_host,
        upstream_port,
    )
    _log(
        f"{spec.name}: listen {spec.listen_host}:{spec.listen_port} -> "
        f"{upstream_host}:{upstream_port}"
    )
    return server


def serve_forever() -> int:
    servers = [server for server in (ensure_bridge(spec) for spec in BRIDGE_SPECS) if server]
    if not servers:
        return 0

    stop_event = threading.Event()

    def _stop(_signum: int, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_forever())
