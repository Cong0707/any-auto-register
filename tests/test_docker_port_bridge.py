import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

from services.docker_port_bridge import BridgeSpec, bridge_target, can_connect, start_bridge_server


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DockerPortBridgeTests(unittest.TestCase):
    def test_bridge_target_reads_env_override(self):
        spec = BridgeSpec(
            name="cliproxyapi",
            listen_host="0.0.0.0",
            listen_port=8317,
            upstream_host_env="CLIPROXYAPI_UPSTREAM_HOST",
            upstream_port_env="CLIPROXYAPI_UPSTREAM_PORT",
            default_upstream_host="host.docker.internal",
            default_upstream_port=8317,
        )

        with mock.patch.dict(
            "os.environ",
            {
                "CLIPROXYAPI_UPSTREAM_HOST": "example.internal",
                "CLIPROXYAPI_UPSTREAM_PORT": "28317",
            },
            clear=False,
        ):
            self.assertEqual(bridge_target(spec), ("example.internal", 28317))

    def test_bridge_target_ignores_invalid_port_override(self):
        spec = BridgeSpec(
            name="cliproxyapi",
            listen_host="0.0.0.0",
            listen_port=8317,
            upstream_host_env="CLIPROXYAPI_UPSTREAM_HOST",
            upstream_port_env="CLIPROXYAPI_UPSTREAM_PORT",
            default_upstream_host="host.docker.internal",
            default_upstream_port=8317,
        )

        with mock.patch.dict(
            "os.environ",
            {
                "CLIPROXYAPI_UPSTREAM_HOST": "example.internal",
                "CLIPROXYAPI_UPSTREAM_PORT": "not-a-port",
            },
            clear=False,
        ):
            self.assertEqual(bridge_target(spec), ("example.internal", 8317))

    def test_bridge_forwards_tcp_payload(self):
        upstream_port = _free_port()
        bridge_port = _free_port()

        upstream = _EchoServer(("127.0.0.1", upstream_port), _EchoHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        bridge = start_bridge_server("test", "127.0.0.1", bridge_port, "127.0.0.1", upstream_port)
        try:
            deadline = time.time() + 3
            while time.time() < deadline and not can_connect("127.0.0.1", bridge_port):
                time.sleep(0.05)
            self.assertTrue(
                can_connect("127.0.0.1", bridge_port),
                f"bridge listener did not become ready on port {bridge_port}",
            )

            with socket.create_connection(("127.0.0.1", bridge_port), timeout=2) as client:
                client.sendall(b"ping")
                chunks = bytearray()
                deadline = time.time() + 2
                while len(chunks) < 4 and time.time() < deadline:
                    packet = client.recv(4 - len(chunks))
                    if not packet:
                        break
                    chunks.extend(packet)
                self.assertEqual(bytes(chunks), b"ping")
        finally:
            bridge.shutdown()
            bridge.server_close()
            upstream.shutdown()
            upstream.server_close()


if __name__ == "__main__":
    unittest.main()
