"""
Проверка входящих сообщений Ozon: кто из продавцов ответил.

Откуда список чатов
-------------------
Страница сообщений (https://www.ozon.ru/my/chat) рендерится виджетом
webMessenger, и его данные Ozon отдаёт композер-API одним JSON'ом:

    /api/composer-api.bx/page/json/v2?url=/my/chat

В ответе — widgetStates["webMessenger-..."], внутри webSocketResponse.
getChats.items[].model.blocks[name="chat"].state: имя собеседника (title),
счётчик непрочитанных (unreadCountIndicator.value) и превью последнего
сообщения. Это ровно то, что нужно, чтобы понять, кто ответил.

Что делает check()
------------------
1. Забирает список чатов из живой сессии (тот же Chrome с профилем парсера).
2. Для чатов с непрочитанными ищет магазин в базе по имени.
3. Нашёл и статус ещё не «ответил» — ставит replied и записывает входящее
   в журнал сообщений (result='received'), чтобы история в карточке
   показывала переписку.
4. Возвращает сводку: сколько чатов с непрочитанными, кого пометили, чьи
   имена не совпали с базой (их видно вручную).

Непрочитанность в самом Ozon не трогаем: пользователь может захотеть
перечитать ответ в браузере.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

CHATS_URL = (
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fmy%2Fchat"
)
STATUS_REPLIED = "replied"


def parse_chats(payload: dict) -> List[dict]:
    """Чаты из ответа композера. Незнакомая разметка — пустой список."""
    states = payload.get("widgetStates") or {}
    raw = next((v for k, v in states.items() if k.startswith("webMessenger-")), None)
    if not raw:
        return []
    try:
        widget = json.loads(raw)
        response = widget.get("webSocketResponse") or {}
        items = (response.get("getChats") or {}).get("items") or []
    except (ValueError, TypeError):
        return []

    chats = []
    for item in items:
        for block in (item.get("model") or {}).get("blocks") or []:
            if block.get("name") != "chat":
                continue
            state = block.get("state") or {}
            indicator = state.get("unreadCountIndicator") or {}
            preview = state.get("lastMessagePreview") or {}
            try:
                unread = int(indicator.get("value") or state.get("unreadCount") or 0)
            except (TypeError, ValueError):
                unread = 0
            chats.append({
                "title": str(state.get("title") or "").strip(),
                "unread": unread,
                "text": str(preview.get("text") or "").strip(),
                "date": str(preview.get("date") or ""),
                "deeplink": str(state.get("deeplink") or ""),
            })
    return chats


class InboxChecker:
    """Снимает список чатов из живой сессии и помечает ответивших."""

    def __init__(self, store):
        self.store = store

    def fetch_chats(self) -> List[dict]:
        """Открывает композер-API в браузере и разбирает список чатов."""
        from ..utils.selenium_manager import SeleniumManager
        from ..outreach.sender import focus_tab

        manager = SeleniumManager(headless=False)
        try:
            driver = manager.create_driver()
            focus_tab(manager)
            driver.get(CHATS_URL)
            time.sleep(5)
            body = driver.find_element("tag name", "body").text
            payload = json.loads(body)
            return parse_chats(payload)
        finally:
            try:
                manager.close()
            except Exception:                                # noqa: BLE001
                pass

    def check(self) -> dict:
        """Полный проход: чаты -> сопоставление -> статусы и журнал."""
        chats = self.fetch_chats()
        marked, unchanged, unmatched, seen = [], [], [], 0
        for chat in chats:
            if chat["unread"] <= 0:
                continue
            seen += 1
            row = self.store.find_seller_by_shop(chat["title"]) if chat["title"] else None
            if row is None:
                unmatched.append(chat["title"])
                logger.warning(f"Чат с непрочитанными не найден в базе: {chat['title']!r}")
                continue
            if row["status"] == STATUS_REPLIED:
                unchanged.append(chat["title"])
                continue
            when = ""
            if chat["date"]:
                try:
                    when = datetime.fromisoformat(
                        chat["date"].replace("Z", "+00:00")).strftime("%d.%m %H:%M")
                except ValueError:
                    when = ""
            text = f"[входящее {when}] {chat['text']}" if when else f"[входящее] {chat['text']}"
            self.store.set_status(row["seller_id"], STATUS_REPLIED)
            self.store.log_message(row["seller_id"], text, "received")
            marked.append({"seller_id": row["seller_id"], "shop": chat["title"],
                           "text": chat["text"]})
            logger.info(f"Ответил: {chat['title']} — {chat['text'][:60]}")

        return {
            "chats": len(chats),
            "unread": seen,
            "marked": marked,
            "unchanged": unchanged,
            "unmatched": unmatched,
        }
