#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба композер-API чатов: отдаёт ли /my/chat JSON из залогиненной сессии.

Запуск:  .venv/bin/python tools/inspect_inbox_api.py
"""

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUT = REPO_ROOT / "logs" / "inbox_api.json"


def main() -> int:
    from src.utils.selenium_manager import SeleniumManager

    urls = [
        "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fmy%2Fchat",
        "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Fmy%2Fchat%2Fmessages",
    ]
    manager = SeleniumManager(headless=False)
    driver = manager.create_driver()
    try:
        driver.get("https://www.ozon.ru/")
        time.sleep(5)
        for url in urls:
            driver.get(url)
            time.sleep(5)
            body = driver.find_element("tag name", "body").text
            print(f"{url}\n  первые 300: {body[:300]!r}")
            try:
                data = json.loads(body)
                OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"  JSON валиден, сохранён: {OUT}, ключей: {len(data)}")
                if isinstance(data, dict):
                    widgets = data.get("widgetStates") or {}
                    print("  widgets:", list(widgets.keys())[:15])
                return 0
            except ValueError:
                print("  не JSON")
        return 1
    finally:
        try:
            manager.close()
        except Exception:                                    # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
