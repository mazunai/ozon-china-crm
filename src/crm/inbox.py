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

Откуда полный текст ответа
--------------------------
Превью в списке чатов может быть обрезано, а по требованиям проекта ответ
продавца сохраняется ЦЕЛИКОМ. Снято с живой страницы 26.09.2026:

* композер-API страницы чата НЕ отдаёт сообщения — только оболочку аккаунта
  (сообщения гоняются по WebSocket);
* прямая ссылка на чат не работает без подписи url_sign (см. AGENTS 5.4);
* работает путь отправщика: страница магазина → кнопка «Написать» →
  открытый чат. В его разметке у каждого сообщения есть атрибут is-mine
  («mine» — наше, пустой — собеседник), текст читается из DOM
  (ChatClient.read_incoming_texts).

Поэтому для чата с непрочитанными открываем его тем же путём, что и
отправка, читаем сообщения продавца и сохраняем целиком. Не вышло —
сохраняем превью: минимум того, что было раньше, не теряется никогда.

Что делает check()
------------------
1. Забирает список чатов из живой сессии (тот же Chrome с профилем парсера).
2. Для чатов с непрочитанными ищет магазин в базе по имени.
3. Нашёл — снимает переписку через «Написать» и сохраняет ответ
   (store.save_reply): текст, дату получения, категорию, статус «ответил».
4. Возвращает сводку: сколько чатов с непрочитанными, кого пометили, чьи
   имена не совпали с базой (их видно вручную).

Открытие чата отмечает сообщения прочитанными в самом Ozon — иначе полный
текст не достать. Ручной перечитывать-флаг не делаем: ответившие чаты и так
считаются разобранными.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .store import REPLY_CATEGORY_LABELS

logger = logging.getLogger(__name__)

CHATS_URL = (
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fmy%2Fchat"
)
STATUS_REPLIED = "replied"

# Служебные сообщения Ozon приходят с пустым is-mine, как настоящие ответы.
# Без фильтра робот сохранил бы подсказку системы как ответ продавца.
SYSTEM_REPLY_MARKERS = ("Ассистент Ozon", "Вы написали продавцу")


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


def parse_composer_date(raw: str) -> str:
    """Дату из ответа Ozon ('2026-09-26T02:00:58.711922Z') — в UTC ISO.

    Не распарсилось — пустая строка: сохранение подставит текущий момент.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def filter_incoming_bubbles(
    bubbles: Iterable[str], own_texts: Iterable[str] = ()
) -> List[str]:
    """Из «чужих» пузырей чата оставляет только настоящие ответы продавца.

    Выкидывает: пустые, служебные сообщения Ozon (SYSTEM_REPLY_MARKERS) и —
    для подстраховки от сбоя разметки is-mine — тексты, совпадающие с тем,
    что мы сами отправляли (журнал messages). Порядок сохраняется.
    """
    own = {" ".join(str(t).split()).lower() for t in own_texts if str(t).strip()}
    kept: List[str] = []
    for bubble in bubbles:
        if bubble is None:
            continue
        text = str(bubble).strip()
        if not text:
            continue
        if " ".join(text.split()).lower() in own:
            continue
        if any(marker in text for marker in SYSTEM_REPLY_MARKERS):
            continue
        kept.append(text)
    return kept


class InboxChecker:
    """Снимает список чатов из живой сессии и сохраняет ответы продавцов."""

    def __init__(self, store, data_dir: Optional[Path] = None,
                 fetch_full_text: bool = True):
        self.store = store
        # data_dir с selectors.json нужен для снятия полного текста: чат
        # открывается тем же путём, что и у отправщика. Без него — только
        # превью из списка чатов.
        self.data_dir = Path(data_dir) if data_dir else None
        self.fetch_full_text = fetch_full_text

    def _open_session(self):
        from ..utils.selenium_manager import SeleniumManager

        manager = SeleniumManager(headless=False)
        return manager, manager.create_driver()

    @staticmethod
    def _close(manager) -> None:
        try:
            manager.close()
        except Exception:                                    # noqa: BLE001
            pass

    @staticmethod
    def _fetch_chat_list(driver) -> List[dict]:
        driver.get(CHATS_URL)
        time.sleep(5)
        body = driver.find_element("tag name", "body").text
        return parse_chats(json.loads(body))

    def _read_full_reply(self, client, seller_id: str) -> List[str]:
        """Полные тексты ответов из открытого чата — по одному на пузырь.

        Пустой список — не вышло (тогда останется превью из списка чатов).
        """
        own = [m["text"] for m in self.store.message_history(seller_id, limit=100)
               if m["result"] == "sent"]
        try:
            client.open_chat(seller_id)
            time.sleep(4)   # переписка дозагружается после открытия
            bubbles = client.read_incoming_texts()
        except Exception as exc:                             # noqa: BLE001
            logger.warning(f"Полный текст чата {seller_id} не получен: {exc}")
            return []
        return filter_incoming_bubbles(bubbles, own)

    def _make_chat_client(self, driver, manager):
        from ..outreach.sender import ChatClient, ChatSelectors

        selectors = ChatSelectors.load(self.data_dir / "selectors.json")
        return ChatClient(driver, selectors, manager=manager)

    def fetch_chats(self) -> List[dict]:
        """Открывает композер-API в браузере и разбирает список чатов."""
        manager, driver = self._open_session()
        try:
            from ..outreach.sender import focus_tab

            focus_tab(manager)
            return self._fetch_chat_list(driver)
        finally:
            self._close(manager)

    def _check_with_session(self, manager, driver, chats) -> dict:
        """Общий проход по чатам: сопоставление и сохранение ответов."""
        marked, unchanged, unmatched, seen = [], [], [], 0
        client = None
        if self.fetch_full_text and self.data_dir:
            try:
                client = self._make_chat_client(driver, manager)
            except Exception as exc:                         # noqa: BLE001
                logger.warning(f"Полные тексты недоступны (селекторы?): {exc}")

        for chat in chats:
            if chat["unread"] <= 0:
                continue
            seen += 1
            row = self.store.find_seller_by_shop(chat["title"]) if chat["title"] else None
            if row is None:
                unmatched.append(chat["title"])
                logger.warning(f"Чат с непрочитанными не найден в базе: {chat['title']!r}")
                continue
            # Продавцы со статусом «ответил» больше НЕ пропускаются: их новые
            # входящие тоже обязаны попасть в журнал (аудит: пропуск молча
            # терял повторные ответы — прямое нарушение «ни один ответ
            # не теряется»).

            received_at = parse_composer_date(chat["date"])
            preview = (chat["text"] or "").strip()
            full_bubbles: List[str] = []
            if client is not None:
                full_bubbles = self._read_full_reply(client, row["seller_id"])

            # Порядок сохранения важен: открытие чата помечает его прочитанным
            # в Ozon, поэтому ПРЕВЬЮ сохраняем ДО открытия, а полный текст
            # дотягиваем update_reply_text (он перезаписывает превью, только
            # если длиннее). Иначе падение после открытия чата теряло ответ
            # навсегда (аудит).
            if not preview and not full_bubbles:
                logger.warning(f"У чата {chat['title']!r} нет текста — пропущен")
                continue

            category = ""
            if preview:
                category = self.store.save_reply(row["seller_id"], preview, received_at)
            for i, bubble in enumerate(full_bubbles):
                if i == 0 and preview:
                    # первый пузырь — тот же ответ целиком: обновляем превью
                    self.store.update_reply_text(row["seller_id"], bubble, received_at)
                else:
                    category = self.store.save_reply(row["seller_id"], bubble, received_at)
            if full_bubbles:
                last = self.store.message_history(row["seller_id"], limit=1)
                if last and last[0]["result"] == "received":
                    category = last[0]["category"] or category

            text_for_log = full_bubbles[0] if full_bubbles else preview
            marked.append({
                "seller_id": row["seller_id"],
                "shop": chat["title"],
                "text": text_for_log,
                "category": category,
                "category_label": REPLY_CATEGORY_LABELS.get(category, category),
            })
            logger.info(f"Ответил: {chat['title']} — {text_for_log[:60]}")

        return {
            "chats": len(chats),
            "unread": seen,
            "marked": marked,
            "unchanged": unchanged,
            "unmatched": unmatched,
        }

    def check(self) -> dict:
        """Полный проход: чаты -> сопоставление -> сохранение ответов."""
        manager, driver = self._open_session()
        try:
            from ..outreach.sender import focus_tab

            focus_tab(manager)
            chats = self._fetch_chat_list(driver)
            return self._check_with_session(manager, driver, chats)
        finally:
            self._close(manager)

    def backfill_texts(self, seller_ids: List[str]) -> dict:
        """Дотягивает полный текст ответов задним числом.

        Для продавцов, помеченных «ответил» до появления полного разбора
        чата: их чат открывается через «Написать», переписка снимается из
        DOM независимо от счётчика непрочитанных. Перезапись — только если
        полный текст длиннее сохранённого (store.update_reply_text).
        """
        wanted = set(seller_ids)
        empty = {"chats": 0, "found": 0, "updated": [], "shorter": [],
                 "no_text": [], "missing": list(seller_ids)}
        if not wanted:
            return empty

        manager, driver = self._open_session()
        try:
            from ..outreach.sender import focus_tab

            focus_tab(manager)
            chats = self._fetch_chat_list(driver)

            client = None
            if self.fetch_full_text and self.data_dir:
                client = self._make_chat_client(driver, manager)

            found, updated, shorter, no_text, seen_ids = 0, [], [], [], set()
            for chat in chats:
                row = self.store.find_seller_by_shop(chat["title"]) if chat["title"] else None
                if row is None or row["seller_id"] not in wanted:
                    continue
                found += 1
                seen_ids.add(row["seller_id"])

                received_at = parse_composer_date(chat["date"])
                full = ""
                if client is not None:
                    full = self._read_full_reply(client, row["seller_id"])
                if not full:
                    no_text.append(chat["title"])
                    continue
                if self.store.update_reply_text(row["seller_id"], full, received_at):
                    updated.append({"shop": chat["title"], "seller_id": row["seller_id"],
                                    "text": full})
                else:
                    shorter.append({"shop": chat["title"], "text": full})
            missing = list(wanted - seen_ids)
            return {"chats": len(chats), "found": found, "updated": updated,
                    "shorter": shorter, "no_text": no_text, "missing": missing}
        finally:
            self._close(manager)
