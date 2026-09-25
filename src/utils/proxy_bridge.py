"""
Локальный мост к прокси с авторизацией.

Chrome не поддерживает логин/пароль в параметре --proxy-server, а большинство
резидентских прокси выдаются как user:pass@host:port. Мост слушает localhost,
принимает CONNECT от Chrome и сам добавляет к запросу Proxy-Authorization,
после чего просто перекачивает байты в обе стороны.

Использование:
    from src.utils.proxy_bridge import start_bridge
    local = start_bridge("http://user:pass@host:port")   # -> "127.0.0.1:PORT"
"""

from __future__ import annotations

import base64
import logging
import re
import select
import socket
import socketserver
import threading
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PROXY_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5)://)?"
    r"(?:(?P<user>[^:@/]+):(?P<password>[^@/]+)@)?"
    r"(?P<host>[^:@/]+):(?P<port>\d+)$"
)


def parse_proxy(value: str) -> Optional[dict]:
    """Разбирает прокси-строку в словарь (scheme/user/password/host/port)."""
    if not value:
        return None
    match = PROXY_RE.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    parts["scheme"] = (parts.get("scheme") or "http").lower()
    parts["port"] = int(parts["port"])
    return parts


class _ConnectHandler(socketserver.BaseRequestHandler):
    upstream_host = ""
    upstream_port = 0
    auth_header = ""

    def handle(self) -> None:
        try:
            head = self.request.recv(65536)
        except Exception:
            return
        if not head:
            return
        first_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if not first_line.upper().startswith("CONNECT"):
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        try:
            target = first_line.split()[1]
        except IndexError:
            return

        try:
            upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)
        except Exception as exc:                      # noqa: BLE001
            logger.warning(f"Мост: не подключиться к прокси: {exc}")
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        request = (
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"Proxy-Authorization: Basic {self.auth_header}\r\n"
            f"Proxy-Connection: Keep-Alive\r\n\r\n"
        )
        upstream.sendall(request.encode("latin-1"))

        try:
            answer = upstream.recv(65536)
        except Exception:
            upstream.close()
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        status = answer.split(b"\r\n", 1)[0]
        if b" 200" not in status:
            logger.warning(f"Мост: прокси отказал: {status.decode('latin-1', 'replace')}")
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            upstream.close()
            return

        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self._pump(self.request, upstream)

    @staticmethod
    def _pump(client: socket.socket, upstream: socket.socket) -> None:
        socks = [client, upstream]
        try:
            while True:
                readable, _, _ = select.select(socks, [], [], 60)
                if not readable:
                    continue
                for sock in readable:
                    other = upstream if sock is client else client
                    try:
                        chunk = sock.recv(65536)
                    except Exception:
                        return
                    if not chunk:
                        return
                    try:
                        other.sendall(chunk)
                    except Exception:
                        return
        finally:
            for sock in socks:
                try:
                    sock.close()
                except Exception:
                    pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], proxy: dict):
        _ConnectHandler.upstream_host = proxy["host"]
        _ConnectHandler.upstream_port = proxy["port"]
        if proxy.get("user"):
            token = f"{proxy['user']}:{proxy.get('password', '')}".encode("utf-8")
            _ConnectHandler.auth_header = base64.b64encode(token).decode("ascii")
        else:
            _ConnectHandler.auth_header = ""
        super().__init__(address, _ConnectHandler)


_bridge_lock = threading.Lock()
_bridge: Optional[_Server] = None
_bridge_address: str = ""


def start_bridge(proxy_value: str) -> str:
    """Поднимает мост и возвращает адрес для --proxy-server (127.0.0.1:PORT).

    Если прокси без авторизации — возвращает исходную строку как есть.
    """
    global _bridge, _bridge_address

    proxy = parse_proxy(proxy_value)
    if not proxy:
        raise ValueError(f"Не разобрать прокси: {proxy_value!r}")
    if proxy["scheme"] == "socks5":
        # socks5 Chrome понимает сам, но авторизацию — нет
        raise ValueError("SOCKS5 с авторизацией не поддерживается, нужен HTTP-прокси")

    if not proxy.get("user"):
        return f"{proxy['host']}:{proxy['port']}"

    with _bridge_lock:
        if _bridge is not None:
            return _bridge_address
        server = _Server(("127.0.0.1", 0), proxy)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _bridge, _bridge_address = server, f"127.0.0.1:{port}"
        logger.info(f"Поднят локальный мост к прокси: {_bridge_address}")
        return _bridge_address
