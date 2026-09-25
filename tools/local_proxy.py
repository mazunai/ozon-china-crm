#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой HTTP CONNECT-прокси для запуска на домашней машине.

Зачем: облачный сервер (VM) Ozon блокирует, а домашний IP — пускает. Прокси
поднимается на домашней машине, а VM подключается к нему через обратный
SSH-туннель:

    # на домашней машине:
    python3 tools/local_proxy.py --port 8888
    ssh -N -R 8888:127.0.0.1:8888 ubuntu@<IP-ВМ>          # туннель на VM

    # на VM в config.txt:
    OZON_PROXY=127.0.0.1:8888

Запуск:  python3 tools/local_proxy.py [--port 8888] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import logging
import select
import socket
import socketserver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("local-proxy")


def _pump(client: socket.socket, upstream: socket.socket) -> None:
    socks = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(socks, [], [], 120)
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


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            try:
                chunk = self.request.recv(4096)
            except Exception:
                return
            if not chunk:
                return
            head += chunk
            if len(head) > 65536:
                return

        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first.split()
        if len(parts) < 2 or not parts[0].upper().startswith("CONNECT"):
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        host, _, port = parts[1].rpartition(":")
        if not host:
            host, port = parts[1], "443"
        try:
            upstream = socket.create_connection((host, int(port)), timeout=30)
        except Exception as exc:                      # noqa: BLE001
            logger.warning(f"не подключиться к {host}:{port} — {exc}")
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        logger.info(f"CONNECT {host}:{port}")
        _pump(self.request, upstream)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный CONNECT-прокси для VM-парсера")
    parser.add_argument("--host", default="127.0.0.1", help="адрес прослушивания")
    parser.add_argument("--port", type=int, default=8888, help="порт")
    args = parser.parse_args()

    with ProxyServer((args.host, args.port), ConnectHandler) as server:
        logger.info(f"Прокси слушает {args.host}:{args.port} — трафик пойдёт через этот Mac")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Остановлено")


if __name__ == "__main__":
    main()
