#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""РЕЗЕРВНЫЙ релей Telegram: на МАКЕ, работает только пока Mac включён.

Основной способ — Cloudflare Worker (deploy/tg-worker.js, всегда онлайн,
от Макa не зависит): см. tools/tg_relay_setup.sh. Этот скрипт — временная
затычка: Mac ходит в Telegram сам и пересылает запросы дашборда.

Как включить (если нужен):
1. На Mac слушает 127.0.0.1:8010.
2. В tools/vm_tunnel.sh добавить обратный форвардинг -R 127.0.0.1:8010.
3. В .env на VM: TELEGRAM_API_BASE=http://127.0.0.1:8010
"""

import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://api.telegram.org"
PORT = 8010


class RelayHandler(BaseHTTPRequestHandler):
    """Прозрачная пересылка: тот же путь, тело и ответ, только через Mac."""

    protocol_version = "HTTP/1.1"

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(UPSTREAM + self.path, data=body,
                                         method=self.command)
        if body:
            request.add_header("Content-Type",
                               self.headers.get("Content-Type", "application/json"))
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data, code = response.read(), response.status
        except urllib.error.HTTPError as exc:
            # Ошибки Telegram (400/409/…) возвращаем клиенту как есть
            data, code = exc.read(), exc.code
        except Exception as exc:                             # noqa: BLE001
            data = json.dumps({"ok": False, "relay_error": str(exc)}).encode("utf-8")
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _forward
    do_POST = _forward

    def log_message(self, fmt, *args):                       # noqa: A002
        # Тихий лог: полно запросов long polling, шуметь в консоль незачем
        pass


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), RelayHandler)
    print(f"TG-релей слушает 127.0.0.1:{port} -> {UPSTREAM}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
