#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты разбора списка чатов (автоотметка «ответил»).

Запуск: python3 test/test_inbox.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.inbox import parse_chats

# Мини-кусок реального ответа композера /my/chat: два чата, у одного
# непрочитанное, у другого всё просмотрено.
SAMPLE = {
    "widgetStates": {
        "webMessenger-1667331-default-1": json.dumps({
            "url": "/my/chat",
            "webSocketResponse": {
                "getChats": {
                    "items": [
                        {"model": {"blocks": [{"name": "chat", "state": {
                            "title": "YM Store",
                            "unreadCountIndicator": {"value": 2, "type": "Active"},
                            "lastMessagePreview": {
                                "text": "Дорогой друг, чем я могу тебе помочь?",
                                "date": "2026-09-26T02:00:58.711922Z",
                            },
                            "deeplink": "/communications/chats/chat?id=abc",
                        }}]}},
                        {"model": {"blocks": [{"name": "chat", "state": {
                            "title": "KJ LED Store",
                            "unreadCountIndicator": {"value": 0, "type": "None"},
                            "lastMessagePreview": {"text": "Спасибо!", "date": ""},
                        }}]}},
                        {"model": {"blocks": [{"name": "other", "state": {}}]}},
                    ],
                },
            },
        }),
    },
}

# Прежняя разметка: unreadCount лежал прямо в state
LEGACY = {
    "widgetStates": {
        "webMessenger-1-default-1": json.dumps({
            "webSocketResponse": {"getChats": {"items": [
                {"model": {"blocks": [{"name": "chat", "state": {
                    "title": "Старый продавец", "unreadCount": 1,
                    "lastMessagePreview": {"text": "привет"},
                }}]}},
            ]}},
        }),
    },
}

# Пустой ответ: виджета нет вовсе
EMPTY = {"widgetStates": {}}


def run() -> int:
    failed = 0

    def check(name, condition):
        nonlocal failed
        print(("OK   " if condition else "FAIL ") + name)
        if not condition:
            failed += 1

    chats = parse_chats(SAMPLE)
    check("найдены оба чата (блок other отфильтрован)", len(chats) == 2)
    check("имя магазина из state.title", chats[0]["title"] == "YM Store")
    check("непрочитанные из unreadCountIndicator", chats[0]["unread"] == 2)
    check("текст последнего сообщения", "чем я могу" in chats[0]["text"])
    check("дата последнего сообщения", chats[0]["date"].startswith("2026-09-26"))
    check("просмотренный чат с unread=0 присутствует", chats[1]["unread"] == 0)

    legacy = parse_chats(LEGACY)
    check("старая разметка: unreadCount в state", len(legacy) == 1 and legacy[0]["unread"] == 1)

    check("пустой ответ даёт пустой список", parse_chats(EMPTY) == [])
    check("мусор вместо JSON не роняет разбор", parse_chats({"widgetStates": {
        "webMessenger-1": "не json"}}) == [])

    print(f"\nВсего: 9, провалено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
