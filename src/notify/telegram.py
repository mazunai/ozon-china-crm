"""
Telegram-уведомления: итоги ночного сбора, ответы продавцов, ошибки.

Токен и chat_id читаются из .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) или
из окружения. Не настроено — функции молча ничего не делают: уведомления
никогда не должны ломать основной процесс.

Отправка через Bot API обычным POST (aiogram для этого не нужен).
"""

import json
import logging
import os
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _credentials() -> tuple:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        return token, chat_id
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return token, chat_id
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key == "TELEGRAM_BOT_TOKEN" and not token:
                token = value.strip()
            elif key == "TELEGRAM_CHAT_ID" and not chat_id:
                chat_id = value.strip()
    except OSError:
        pass
    return token, chat_id


def send(text: str) -> bool:
    """Шлёт сообщение в чат. False — не настроено или не ушло (не бросает)."""
    token, chat_id = _credentials()
    if not token or not chat_id:
        return False
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except Exception as exc:                                 # noqa: BLE001
        logger.warning(f"Telegram не отправил: {exc}")
        return False
