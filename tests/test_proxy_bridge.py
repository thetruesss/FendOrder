"""Acceptance checklist §8: the local HTTP-to-SOCKS5 bridge.

This is an end-to-end test, not a mock: it stands up a real SOCKS5 server that
*requires* username/password authentication, points the bridge at it, and drives
traffic through the bridge exactly as Camoufox would — unauthenticated HTTP on
loopback. If the bridge failed to attach credentials, the SOCKS5 server would
reject the handshake and every assertion below would fail.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ozon_anti_bot_engine.core.proxy_manager import HttpToSocks5Bridge, ProxyEndpoint

SOCKS_USER = "resi-user"
SOCKS_PASS = "s3cr3t:pass/word"  # exercises URI-escaping too


# =============================================================================
#  Test doubles
# =============================================================================
class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        body = f"pong:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise ConnectionError("socks peer closed early")
        buffer += chunk
    return buffer


class _Socks5Handler(socketserver.BaseRequestHandler):
    """Minimal RFC 1928 + RFC 1929 server that only accepts user/pass auth."""

    def handle(self) -> None:
        conn = self.request
        server = self.server  # type: ignore[assignment]

        # --- greeting -------------------------------------------------
        version, method_count = _recv_exact(conn, 2)
        methods = _recv_exact(conn, method_count)
        if version != 0x05 or 0x02 not in methods:
            conn.sendall(b"\x05\xff")
            server.rejected_no_auth += 1  # type: ignore[attr-defined]
            return
        conn.sendall(b"\x05\x02")  # demand username/password

        # --- RFC 1929 auth --------------------------------------------
        _auth_version, ulen = _recv_exact(conn, 2)
        username = _recv_exact(conn, ulen).decode()
        plen = _recv_exact(conn, 1)[0]
        password = _recv_exact(conn, plen).decode()
        server.auth_attempts.append((username, password))  # type: ignore[attr-defined]

        if (username, password) != (SOCKS_USER, SOCKS_PASS):
            conn.sendall(b"\x01\x01")
            return
        conn.sendall(b"\x01\x00")

        # --- CONNECT request ------------------------------------------
        _ver, command, _rsv, address_type = _recv_exact(conn, 4)
        if address_type == 0x01:
            host = socket.inet_ntoa(_recv_exact(conn, 4))
        elif address_type == 0x03:
            length = _recv_exact(conn, 1)[0]
            host = _recv_exact(conn, length).decode()
        else:
            conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return
        port = int.from_bytes(_recv_exact(conn, 2), "big")

        if command != 0x01:
            conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return

        try:
            remote = socket.create_connection((host, port), timeout=10)
        except OSError:
            conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return

        server.tunnels.append((host, port))  # type: ignore[attr-defined]
        conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")

        with remote:
            self._relay(conn, remote)

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        import select

        sockets = [left, right]
        while True:
            readable, _, errored = select.select(sockets, [], sockets, 10)
            if errored or not readable:
                return
            for sock in readable:
                try:
                    data = sock.recv(65_536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (right if sock is left else left).sendall(data)
                except OSError:
                    return


class _Socks5Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr) -> None:
        super().__init__(addr, _Socks5Handler)
        self.auth_attempts: list[tuple[str, str]] = []
        self.tunnels: list[tuple[str, int]] = []
        self.rejected_no_auth = 0


# =============================================================================
#  Fixtures
# =============================================================================
@pytest.fixture
def origin():
    server = HTTPServer(("127.0.0.1", 0), _OriginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def socks_server():
    server = _Socks5Server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def bridge(socks_server):
    from urllib.parse import quote

    uri = (
        f"socks5://{quote(SOCKS_USER, safe='')}:{quote(SOCKS_PASS, safe='')}"
        f"@127.0.0.1:{socks_server.server_address[1]}"
    )
    instance = HttpToSocks5Bridge(ProxyEndpoint.parse(uri), port=0)
    instance.start()
    yield instance
    instance.stop()


def _http_through_bridge(bridge_port: int, request: bytes, read_timeout: float = 10) -> bytes:
    with socket.create_connection(("127.0.0.1", bridge_port), timeout=10) as sock:
        sock.settimeout(read_timeout)
        sock.sendall(request)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65_536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


# =============================================================================
#  Tests
# =============================================================================
def test_absolute_form_request_traverses_the_authenticated_socks5_upstream(
    origin, socks_server, bridge
) -> None:
    origin_port = origin.server_address[1]
    response = _http_through_bridge(
        bridge.port,
        f"GET http://127.0.0.1:{origin_port}/ping HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{origin_port}\r\n"
        f"Connection: close\r\n\r\n".encode(),
    )

    assert b"200 OK" in response
    assert b"pong:/ping" in response
    # The credentials the browser never saw were supplied by the bridge.
    assert socks_server.auth_attempts == [(SOCKS_USER, SOCKS_PASS)]
    assert socks_server.tunnels[0][1] == origin_port


def test_connect_tunnel_traverses_the_authenticated_socks5_upstream(
    origin, socks_server, bridge
) -> None:
    """The CONNECT path — what Gecko uses for every https:// request."""
    origin_port = origin.server_address[1]
    with socket.create_connection(("127.0.0.1", bridge.port), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(
            f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin_port}\r\n\r\n".encode()
        )

        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        assert b"200 Connection established" in head

        # Speak plain HTTP inside the tunnel; the bridge must be transparent.
        sock.sendall(
            f"GET /tunnelled HTTP/1.1\r\nHost: 127.0.0.1:{origin_port}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        body = b""
        while True:
            try:
                chunk = sock.recv(65_536)
            except TimeoutError:
                break
            if not chunk:
                break
            body += chunk

    assert b"pong:/tunnelled" in body
    assert socks_server.auth_attempts == [(SOCKS_USER, SOCKS_PASS)]


def test_bridge_reports_502_when_no_upstream_is_configured(origin) -> None:
    instance = HttpToSocks5Bridge(upstream=None, port=0)
    instance.start()
    try:
        response = _http_through_bridge(
            instance.port, b"GET http://example.invalid/ HTTP/1.1\r\n\r\n"
        )
        assert b"502" in response
    finally:
        instance.stop()


def test_upstream_can_be_swapped_at_runtime(socks_server, bridge) -> None:
    """Rotation replaces the exit without restarting the listener."""
    original_url = bridge.url
    replacement = ProxyEndpoint.parse("socks5://other:pw@127.0.0.1:1")
    bridge.set_upstream(replacement)

    assert bridge.current_upstream == replacement
    assert bridge.url == original_url  # the browser keeps the same proxy address


def test_bridge_start_is_idempotent(bridge) -> None:
    assert bridge.start() == bridge.url
    assert bridge.is_running


def test_stopped_bridge_refuses_connections(socks_server) -> None:
    instance = HttpToSocks5Bridge(port=0)
    instance.start()
    port = instance.port
    instance.stop()

    assert not instance.is_running
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
