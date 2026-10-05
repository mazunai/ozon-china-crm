"""Командный Telegram-бот: long polling getUpdates на чистой стандартной
библиотеке, без aiogram.

Зачем не aiogram: он уже в requirements для легаси-бота (src/telegram/
bot_manager.py, старый GUI-стек AppManager), но тот не связан с дашбордом.
Новому боту нужны три команды и один long-poll цикл — тянуть фреймворк ради
этого незачем, urllib достаточно.

Важно: ОДИН процесс должен опрашивать getUpdates — два поллера начнут
воровать апдейты друг у друга (Telegram отдаёт сообщение первому). Если
запущен дашборд с ботом, legacy `python bot.py` запускать не нужно.

Безопасность: команды принимаются только из чата, совпадающего с
TELEGRAM_CHAT_ID. Все остальные чаты молча игнорируются.
"""

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from . import telegram as tg

logger = logging.getLogger(__name__)


def parse_command(text: str) -> Tuple[str, str]:
    """Разбирает '/команда аргумент' -> ('/команда', 'аргумент').

    Устойчив к адресации в группах ('/status@my_bot') и лишним пробелам.
    Не команда — возвращает ('', текст).
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", stripped
    parts = stripped.split(None, 1)
    head = parts[0].split("@", 1)[0]
    tail = parts[1].strip() if len(parts) > 1 else ""
    return head.lower(), tail


class TgPoller:
    """Фоновый поток long polling. handler(text) -> str (ответ или '')."""

    def __init__(self, *, handler: Callable[[str], str],
                 allowed_chat_id: str = "",
                 poll_timeout: int = 25,
                 error_pause: float = 5.0):
        self.handler = handler
        self.allowed_chat_id = str(allowed_chat_id or "").strip()
        self.poll_timeout = poll_timeout
        self.error_pause = error_pause
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tg-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- Telegram API -------------------------------------------------------- #

    def _api(self, method: str, payload: Optional[dict] = None,
             timeout: int = 30) -> Optional[dict]:
        token = tg._credentials()[0]
        if not token:
            return None
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": tg.USER_AGENT}
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{tg._api_base()}/bot{token}/{method}",
            data=data,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:                             # noqa: BLE001
            logger.warning(f"Telegram {method} не ответил: {exc}")
            return None

    def _loop(self) -> None:
        logger.info("Telegram-поллер запущен")
        offset = 0
        while not self._stop.is_set():
            answer = self._api(
                "getUpdates",
                {"offset": offset, "timeout": self.poll_timeout,
                 "allowed_updates": ["message"]},
                timeout=self.poll_timeout + 10,
            )
            if self._stop.is_set():
                break
            if not answer or not answer.get("ok"):
                # Сеть дрогнула или токен не тот: пауза и заново, цикл не умирает
                self._stop.wait(self.error_pause)
                continue
            for update in answer.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                text = str(message.get("text") or "")
                chat_id = str(message.get("chat", {}).get("id", ""))
                if not text:
                    continue
                if self.allowed_chat_id and chat_id != self.allowed_chat_id:
                    logger.warning(f"Команда из чужого чата {chat_id} — игнор")
                    continue
                try:
                    reply = self.handler(text)
                except Exception as exc:                     # noqa: BLE001
                    logger.exception("Обработчик Telegram-команды упал")
                    reply = f"Ошибка обработки команды: {exc}"
                if reply:
                    tg.send(reply)
        logger.info("Telegram-поллер остановлен")
