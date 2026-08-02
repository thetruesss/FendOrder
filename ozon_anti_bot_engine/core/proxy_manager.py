"""Proxy layer: a local HTTP-to-SOCKS5 bridge plus a rotating, health-checked pool.

Why the bridge exists (spec §3.2)
---------------------------------
Firefox/Gecko — and therefore Camoufox — cannot authenticate to a SOCKS5 proxy
from a URI. ``network.proxy.socks_username`` does not exist; Gecko only speaks
unauthenticated SOCKS5, and the ``socks5://user:pass@host`` form that curl
accepts is silently dropped. Commercial residential pools are almost always
user/password authenticated, so the browser half of the engine would be unable
to use the same exits as the HTTP half.

The fix is a loopback HTTP proxy that the browser *can* use unauthenticated: it
accepts ``CONNECT`` (and absolute-form) requests on 127.0.0.1 and relays them
through the authenticated SOCKS5 upstream using PySocks. The browser sees a
plain, credential-free HTTP proxy; the exit IP is the residential one.

The listener binds to the loopback interface only and is therefore not exposed
off-host; it deliberately implements no client-side authentication.
"""

from __future__ import annotations

import base64
import logging
import select
import socket
import socketserver
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_SOCKS_SCHEMES = {"socks5", "socks5h", "socks4", "socks4a"}
_HTTP_SCHEMES = {"http", "https"}

# Statuses that must retire the current exit IP (spec §3.2).
ROTATE_ON_STATUS: frozenset[int] = frozenset({403, 429, 503})


# =============================================================================
#  Endpoint
# =============================================================================
@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """A single upstream proxy. Credentials never reach ``__str__``/logs."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, uri: str) -> ProxyEndpoint:
        raw = uri.strip()
        if "://" not in raw:
            raw = f"socks5://{raw}"
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "socks5").lower()
        if scheme not in _SOCKS_SCHEMES | _HTTP_SCHEMES:
            raise ValueError(f"Unsupported proxy scheme: {scheme!r}")
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"Proxy URI must include host and port: {uri!r}")
        return cls(
            scheme=scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )

    @property
    def is_socks(self) -> bool:
        return self.scheme in _SOCKS_SCHEMES

    @property
    def uri(self) -> str:
        """Full URI including credentials — for curl_cffi, never for logs."""
        if self.username:
            from urllib.parse import quote

            auth = quote(self.username, safe="")
            if self.password:
                auth = f"{auth}:{quote(self.password, safe='')}"
            return f"{self.scheme}://{auth}@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def safe_uri(self) -> str:
        """Redacted form, safe to log."""
        auth = f"{self.username[:2]}***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.safe_uri


# =============================================================================
#  Upstream dialling
# =============================================================================
def _dial_through_socks(
    upstream: ProxyEndpoint, target_host: str, target_port: int, timeout: float
) -> socket.socket:
    import socks  # PySocks

    proxy_type = socks.SOCKS4 if upstream.scheme.startswith("socks4") else socks.SOCKS5
    sock = socks.socksocket()
    sock.settimeout(timeout)
    sock.set_proxy(
        proxy_type,
        upstream.host,
        upstream.port,
        # rdns=True keeps DNS resolution on the exit node. Resolving locally
        # would leak the real client's DNS and, worse, geo-resolve Ozon's CDN
        # against the wrong region.
        rdns=True,
        username=upstream.username,
        password=upstream.password,
    )
    sock.connect((target_host, target_port))
    return sock


def _dial_through_http(
    upstream: ProxyEndpoint, target_host: str, target_port: int, timeout: float
) -> socket.socket:
    sock = socket.create_connection((upstream.host, upstream.port), timeout=timeout)
    request = [f"CONNECT {target_host}:{target_port} HTTP/1.1",
               f"Host: {target_host}:{target_port}"]
    if upstream.username:
        token = base64.b64encode(
            f"{upstream.username}:{upstream.password or ''}".encode()
        ).decode()
        request.append(f"Proxy-Authorization: Basic {token}")
    request.append("Proxy-Connection: Keep-Alive")
    sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError("Upstream HTTP proxy closed during CONNECT")
        response += chunk
        if len(response) > 65_536:
            sock.close()
            raise ConnectionError("Upstream HTTP proxy sent an oversized CONNECT reply")

    status_line = response.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200" not in status_line:
        sock.close()
        raise ConnectionError(f"Upstream CONNECT refused: {status_line}")
    return sock


def dial(
    upstream: ProxyEndpoint, target_host: str, target_port: int, timeout: float = 30.0
) -> socket.socket:
    """Open a TCP tunnel to ``target`` through ``upstream``."""
    if upstream.is_socks:
        return _dial_through_socks(upstream, target_host, target_port, timeout)
    return _dial_through_http(upstream, target_host, target_port, timeout)


# =============================================================================
#  Bridge server
# =============================================================================
class _BoundedThreadingTCPServer(socketserver.TCPServer):
    """TCPServer with a bounded worker pool instead of unbounded thread spawn."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler_cls, max_threads: int) -> None:
        super().__init__(addr, handler_cls)
        self._pool = ThreadPoolExecutor(
            max_workers=max(4, max_threads), thread_name_prefix="proxy-bridge"
        )

    def process_request(self, request, client_address) -> None:  # noqa: D102
        self._pool.submit(self._run, request, client_address)

    def _run(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # pragma: no cover - defensive
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address) -> None:
        logger.debug("proxy bridge connection error from %s", client_address, exc_info=True)

    def server_close(self) -> None:
        super().server_close()
        self._pool.shutdown(wait=False, cancel_futures=True)


class _BridgeHandler(socketserver.StreamRequestHandler):
    """Handles one browser connection: CONNECT tunnel or absolute-form request."""

    # Unbuffered read side so that, once the header block is consumed, every
    # remaining byte is still on the raw socket for the relay loop to pump.
    rbufsize = 0
    timeout = 120

    bridge: HttpToSocks5Bridge  # injected via the handler subclass

    def handle(self) -> None:
        try:
            request_line = self.rfile.readline(65_536)
        except (TimeoutError, OSError):
            return
        if not request_line:
            return

        try:
            method, target, _version = request_line.decode("latin-1").split(maxsplit=2)
        except ValueError:
            self._reply(400, "Bad Request")
            return

        headers: list[str] = []
        while True:
            line = self.rfile.readline(65_536)
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line.decode("latin-1").rstrip("\r\n"))
            if len(headers) > 200:
                self._reply(431, "Request Header Fields Too Large")
                return

        upstream = self.bridge.current_upstream
        if upstream is None:
            self._reply(502, "No upstream proxy configured")
            return

        if method.upper() == "CONNECT":
            self._handle_connect(target, upstream)
        else:
            self._handle_absolute(method, target, headers, upstream)

    # ------------------------------------------------------------------
    def _handle_connect(self, target: str, upstream: ProxyEndpoint) -> None:
        host, _, port_str = target.rpartition(":")
        if not host:
            self._reply(400, "Bad CONNECT target")
            return
        try:
            port = int(port_str)
        except ValueError:
            self._reply(400, "Bad CONNECT port")
            return

        try:
            remote = dial(upstream, host, port, self.bridge.dial_timeout)
        except Exception as exc:
            logger.warning("bridge: CONNECT %s via %s failed: %s", target, upstream, exc)
            self.bridge.note_dial_failure(upstream)
            self._reply(502, "Upstream connect failed")
            return

        self.bridge.note_dial_success(upstream)
        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._relay(self.connection, remote)
        finally:
            _close(remote)

    def _handle_absolute(
        self, method: str, target: str, headers: list[str], upstream: ProxyEndpoint
    ) -> None:
        parsed = urlparse(target)
        if not parsed.hostname:
            self._reply(400, "Absolute-form URI required")
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            remote = dial(upstream, parsed.hostname, port, self.bridge.dial_timeout)
        except Exception as exc:
            logger.warning("bridge: %s %s via %s failed: %s", method, target, upstream, exc)
            self.bridge.note_dial_failure(upstream)
            self._reply(502, "Upstream connect failed")
            return

        self.bridge.note_dial_success(upstream)
        try:
            # Rewrite absolute-form to origin-form and drop hop-by-hop headers.
            forwarded = [f"{method} {path} HTTP/1.1"]
            forwarded += [
                h for h in headers
                if not h.lower().startswith(("proxy-connection:", "proxy-authorization:"))
            ]
            remote.sendall(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin-1"))
            self._relay(self.connection, remote)
        finally:
            _close(remote)

    # ------------------------------------------------------------------
    def _relay(self, client: socket.socket, remote: socket.socket) -> None:
        """Pump bytes both ways until either side hangs up."""
        client.setblocking(False)
        remote.setblocking(False)
        sockets = [client, remote]
        idle_deadline = time.monotonic() + self.bridge.idle_timeout

        while True:
            if time.monotonic() > idle_deadline:
                return
            try:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)
            except (OSError, ValueError):
                return
            if errored:
                return
            for sock in readable:
                try:
                    data = sock.recv(65_536)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                peer = remote if sock is client else client
                try:
                    peer.sendall(data)
                except OSError:
                    return
                idle_deadline = time.monotonic() + self.bridge.idle_timeout

    def _reply(self, status: int, message: str) -> None:
        try:
            self.connection.sendall(
                f"HTTP/1.1 {status} {message}\r\nContent-Length: 0\r\n"
                f"Connection: close\r\n\r\n".encode("latin-1")
            )
        except OSError:
            pass


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class HttpToSocks5Bridge:
    """Loopback HTTP proxy relaying through an authenticated SOCKS5/HTTP upstream.

    The upstream can be swapped at runtime (``set_upstream``); the change applies
    to connections opened afterwards, so an in-flight page load is never torn
    mid-request by a rotation.
    """

    def __init__(
        self,
        upstream: ProxyEndpoint | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        max_threads: int = 64,
        dial_timeout: float = 30.0,
        idle_timeout: float = 90.0,
    ) -> None:
        self._upstream = upstream
        self._lock = threading.RLock()
        self.host = host
        self.requested_port = port
        self.max_threads = max_threads
        self.dial_timeout = dial_timeout
        self.idle_timeout = idle_timeout

        self._server: _BoundedThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None
        self.dial_successes = 0
        self.dial_failures = 0

    # ------------------------------------------------------------------
    @property
    def current_upstream(self) -> ProxyEndpoint | None:
        with self._lock:
            return self._upstream

    def set_upstream(self, upstream: ProxyEndpoint | None) -> None:
        with self._lock:
            previous = self._upstream
            self._upstream = upstream
        if previous != upstream:
            logger.info("proxy bridge upstream -> %s", upstream or "direct")

    def note_dial_success(self, _upstream: ProxyEndpoint) -> None:
        self.dial_successes += 1

    def note_dial_failure(self, _upstream: ProxyEndpoint) -> None:
        self.dial_failures += 1

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("Bridge is not started")
        return self._bound_port

    @property
    def url(self) -> str:
        """The proxy URL to hand to the browser, e.g. ``http://127.0.0.1:51234``."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        if self._server is not None:
            return self.url

        handler_cls = type("_BoundHandler", (_BridgeHandler,), {"bridge": self})
        self._server = _BoundedThreadingTCPServer(
            (self.host, self.requested_port), handler_cls, self.max_threads
        )
        self._bound_port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="proxy-bridge-acceptor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "HTTP-to-SOCKS5 bridge listening on %s -> %s",
            self.url,
            self.current_upstream or "direct",
        )
        return self.url

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self._bound_port = None
        logger.info("HTTP-to-SOCKS5 bridge stopped")

    def __enter__(self) -> HttpToSocks5Bridge:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


# =============================================================================
#  Pool
# =============================================================================
@dataclass
class _ProxyState:
    endpoint: ProxyEndpoint
    banned_until: float = 0.0
    failures: int = 0
    successes: int = 0
    last_used: float = 0.0
    last_status: int | None = None
    healthy: bool = True

    @property
    def available(self) -> bool:
        return self.healthy and time.monotonic() >= self.banned_until


@dataclass
class _StickyLease:
    endpoint: ProxyEndpoint
    expires_at: float


class ProxyPool:
    """Round-robin pool with cooldown bans, sticky sessions and health checks.

    ``acquire(session_id=...)`` pins one exit IP for the whole scrape chain
    (spec §3.2), which matters because Ozon binds region/logistics state to the
    session — hopping IPs mid-chain produces inconsistent prices.

    With an empty pool every method degrades to "direct connection" (``None``)
    so the engine still runs locally without residential proxies.
    """

    def __init__(
        self,
        uris: Iterable[str] = (),
        *,
        cooldown_seconds: int = 180,
        sticky_ttl_seconds: int = 900,
        max_failures: int = 5,
    ) -> None:
        self._states: list[_ProxyState] = []
        for uri in uris:
            try:
                self._states.append(_ProxyState(ProxyEndpoint.parse(uri)))
            except ValueError as exc:
                logger.error("skipping malformed proxy entry: %s", exc)

        self.cooldown_seconds = cooldown_seconds
        self.sticky_ttl_seconds = sticky_ttl_seconds
        self.max_failures = max_failures
        self._cursor = 0
        self._lock = threading.RLock()
        self._sticky: dict[str, _StickyLease] = {}

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._states)

    @property
    def is_empty(self) -> bool:
        return not self._states

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._states if s.available)

    # ------------------------------------------------------------------
    def acquire(self, session_id: str | None = None) -> ProxyEndpoint | None:
        """Lease a proxy. With ``session_id`` the same exit is reused until TTL."""
        if self.is_empty:
            return None

        now = time.monotonic()
        with self._lock:
            if session_id is not None:
                lease = self._sticky.get(session_id)
                if lease is not None:
                    if lease.expires_at > now and self._state_of(lease.endpoint).available:
                        return lease.endpoint
                    self._sticky.pop(session_id, None)

            endpoint = self._next_available(now)
            if endpoint is None:
                return None
            if session_id is not None:
                self._sticky[session_id] = _StickyLease(
                    endpoint, now + self.sticky_ttl_seconds
                )
            return endpoint

    def _next_available(self, now: float) -> ProxyEndpoint | None:
        count = len(self._states)
        for offset in range(count):
            state = self._states[(self._cursor + offset) % count]
            if state.available:
                self._cursor = (self._cursor + offset + 1) % count
                state.last_used = now
                return state.endpoint

        # Everything is benched. Rather than fail the scrape, release the entry
        # whose cooldown expires first — a throttled proxy beats no proxy.
        soonest = min(self._states, key=lambda s: s.banned_until)
        soonest.banned_until = 0.0
        soonest.healthy = True
        soonest.failures = 0
        logger.warning("all proxies benched; force-releasing %s", soonest.endpoint)
        return soonest.endpoint

    # ------------------------------------------------------------------
    def _state_of(self, endpoint: ProxyEndpoint) -> _ProxyState:
        for state in self._states:
            if state.endpoint == endpoint:
                return state
        raise KeyError(f"Unknown proxy: {endpoint}")

    def report_success(self, endpoint: ProxyEndpoint | None) -> None:
        if endpoint is None:
            return
        with self._lock:
            try:
                state = self._state_of(endpoint)
            except KeyError:
                return
            state.successes += 1
            state.failures = 0
            state.healthy = True
            state.last_status = 200

    def report_failure(
        self, endpoint: ProxyEndpoint | None, status: int | None = None
    ) -> bool:
        """Record a failure. Returns ``True`` if the proxy was benched."""
        if endpoint is None:
            return False
        with self._lock:
            try:
                state = self._state_of(endpoint)
            except KeyError:
                return False
            state.failures += 1
            state.last_status = status

            if status in ROTATE_ON_STATUS or state.failures >= self.max_failures:
                state.banned_until = time.monotonic() + self.cooldown_seconds
                logger.warning(
                    "benching %s for %ss (status=%s, failures=%s)",
                    state.endpoint, self.cooldown_seconds, status, state.failures,
                )
                # A benched proxy must not stay pinned to a sticky session.
                for key, lease in list(self._sticky.items()):
                    if lease.endpoint == endpoint:
                        self._sticky.pop(key, None)
                return True
            return False

    def rotate(self, session_id: str | None = None) -> ProxyEndpoint | None:
        """Drop the sticky pin (if any) and hand out the next exit."""
        if session_id is not None:
            with self._lock:
                self._sticky.pop(session_id, None)
        return self.acquire(session_id)

    def release(self, session_id: str) -> None:
        with self._lock:
            self._sticky.pop(session_id, None)

    # ------------------------------------------------------------------
    async def health_check(
        self, url: str, timeout: int = 15, impersonate: str = "chrome124"
    ) -> dict[str, bool]:
        """Probe every proxy against ``url``; mark the failures unhealthy."""
        import asyncio

        from curl_cffi import requests as cffi_requests

        async def probe(state: _ProxyState) -> tuple[str, bool]:
            proxies = {"http": state.endpoint.uri, "https": state.endpoint.uri}
            try:
                async with cffi_requests.AsyncSession() as session:
                    response = await session.get(
                        url,
                        proxies=proxies,
                        timeout=timeout,
                        impersonate=impersonate,
                        allow_redirects=True,
                    )
                ok = response.status_code < 400
            except Exception as exc:
                logger.info("health check failed for %s: %s", state.endpoint, exc)
                ok = False
            with self._lock:
                state.healthy = ok
                if not ok:
                    state.banned_until = time.monotonic() + self.cooldown_seconds
            return state.endpoint.safe_uri, ok

        results = await asyncio.gather(*(probe(s) for s in self._states))
        return dict(results)

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "total": len(self._states),
                "available": sum(1 for s in self._states if s.available),
                "benched": sum(1 for s in self._states if not s.available),
                "sticky_sessions": len(self._sticky),
                "proxies": [
                    {
                        "proxy": s.endpoint.safe_uri,
                        "healthy": s.healthy,
                        "available": s.available,
                        "successes": s.successes,
                        "failures": s.failures,
                        "last_status": s.last_status,
                    }
                    for s in self._states
                ],
            }
