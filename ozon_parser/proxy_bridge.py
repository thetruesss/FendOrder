"""Asynchronous HTTP-to-SOCKS5 bridge.

Camoufox (Firefox core, driven through Playwright) cannot pass a
username/password to an upstream SOCKS5 proxy: Playwright's ``proxy`` option
silently drops SOCKS credentials. The standard work-around is to run a local
plain-HTTP proxy that terminates the browser's ``CONNECT`` tunnel and re-opens
it through the authenticated SOCKS5 exit ourselves, using PySocks.

The bridge listens on ``127.0.0.1:8889`` (see :class:`~ozon_parser.config.BridgeConfig`).
The accept loop is ``asyncio`` based; the blocking SOCKS5 handshake performed by
``socks.socksocket`` runs in the default thread-pool executor so it never stalls
the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import socks  # PySocks

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_CONNECT_TIMEOUT = 30


class SocksHttpBridge:
    """Local HTTP ``CONNECT`` proxy backed by an authenticated SOCKS5 upstream."""

    def __init__(
        self,
        *,
        socks_host: str,
        socks_port: int,
        socks_user: str,
        socks_pass: str,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8889,
    ) -> None:
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.socks_user = socks_user
        self.socks_pass = socks_pass
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server: Optional[asyncio.AbstractServer] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> "SocksHttpBridge":
        if self._server is not None:
            return self
        self._server = await asyncio.start_server(
            self._handle_client, self.listen_host, self.listen_port
        )
        sockets = self._server.sockets or ()
        bound = ", ".join(str(s.getsockname()) for s in sockets)
        logger.info(
            "HTTP->SOCKS5 bridge listening on %s (upstream %s:%s)",
            bound,
            self.socks_host,
            self.socks_port,
        )
        return self

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("HTTP->SOCKS5 bridge stopped")

    async def __aenter__(self) -> "SocksHttpBridge":
        return await self.start()

    async def __aexit__(self, *_exc) -> None:
        await self.stop()

    # -- connection handling ----------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _rest = self._parse_request_line(request_line)

            # Drain the remaining request headers up to the blank line.
            headers = await self._read_headers(reader)

            if method == "CONNECT":
                await self._handle_connect(reader, writer, target)
            else:
                await self._handle_plain_http(
                    reader, writer, method, target, request_line, headers
                )
        except _BridgeError as exc:
            logger.debug("Bridge client %s rejected: %s", peer, exc)
            await self._reply(writer, exc.status, exc.reason)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # pragma: no cover - defensive guard for the accept loop
            logger.exception("Unexpected error handling client %s", peer)
        finally:
            await self._safe_close(writer)

    async def _handle_connect(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target: str,
    ) -> None:
        host, port = self._split_authority(target, default_port=443)
        up_reader, up_writer = await self._open_upstream(host, port)

        # Tell the browser the tunnel is ready; it only sends TLS after this.
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        await self._relay(client_reader, client_writer, up_reader, up_writer)

    async def _handle_plain_http(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        method: str,
        target: str,
        request_line: bytes,
        headers: bytes,
    ) -> None:
        # Ozon is HTTPS-only so this path is rarely exercised, but keeping it
        # makes the bridge a correct general-purpose HTTP proxy.
        host, port = self._authority_from_absolute_uri(target)
        up_reader, up_writer = await self._open_upstream(host, port)

        # Rewrite the origin-form request line (strip scheme://host) and forward.
        path = self._origin_form(target)
        up_writer.write(f"{method} {path} HTTP/1.1\r\n".encode("latin-1"))
        up_writer.write(headers)
        await up_writer.drain()

        await self._relay(client_reader, client_writer, up_reader, up_writer)

    # -- upstream ----------------------------------------------------------

    async def _open_upstream(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        try:
            sock = await loop.run_in_executor(None, self._socks_connect, host, port)
        except Exception as exc:  # PySocks raises socks.ProxyError / OSError
            raise _BridgeError(502, "Bad Gateway", f"upstream connect failed: {exc}") from exc
        return await asyncio.open_connection(sock=sock)

    def _socks_connect(self, host: str, port: int) -> socks.socksocket:
        """Blocking SOCKS5 handshake, executed off the event loop."""

        sock = socks.socksocket()
        sock.set_proxy(
            socks.SOCKS5,
            self.socks_host,
            self.socks_port,
            rdns=True,  # resolve the destination at the exit node, not locally
            username=self.socks_user,
            password=self.socks_pass,
        )
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect((host, port))
        sock.settimeout(None)
        return sock

    # -- byte pump ---------------------------------------------------------

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        await asyncio.gather(
            self._pump(client_reader, up_writer),
            self._pump(up_reader, client_writer),
        )
        await self._safe_close(up_writer)

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await reader.read(_CHUNK)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            # Half-close the write side so the peer sees EOF and unwinds.
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except (OSError, RuntimeError):
                pass

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_request_line(line: bytes) -> tuple[str, str, str]:
        try:
            method, target, version = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
        except ValueError as exc:
            raise _BridgeError(400, "Bad Request", "malformed request line") from exc
        return method.upper(), target, version

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> bytes:
        buf = bytearray()
        while True:
            line = await reader.readline()
            buf += line
            if line in (b"\r\n", b"\n", b""):
                break
        return bytes(buf)

    @staticmethod
    def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
        if ":" in authority:
            host, _, port = authority.rpartition(":")
            return host, int(port)
        return authority, default_port

    @classmethod
    def _authority_from_absolute_uri(cls, uri: str) -> tuple[str, int]:
        without_scheme = uri.split("://", 1)[-1]
        authority = without_scheme.split("/", 1)[0]
        return cls._split_authority(authority, default_port=80)

    @staticmethod
    def _origin_form(uri: str) -> str:
        without_scheme = uri.split("://", 1)[-1]
        slash = without_scheme.find("/")
        return without_scheme[slash:] if slash != -1 else "/"

    async def _reply(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\n\r\n".encode("latin-1"))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    @staticmethod
    async def _safe_close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, RuntimeError, OSError):
            pass


class _BridgeError(Exception):
    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason
