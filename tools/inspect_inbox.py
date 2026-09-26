#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Одноразовая разведка раздела чатов Ozon (для автоотметки «ответил»).

Открывает https://www.ozon.ru/my/chats в профиле парсера и снимает разметку:
полный HTML — в logs/inbox_dump.html, резюме в stdout. По дампу подбираются
селекторы списка чатов и признак непрочитанного.

Запуск на ВМ:  .venv/bin/python tools/inspect_inbox.py
"""

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUT = REPO_ROOT / "logs" / "inbox_dump.html"


def main() -> int:
    from src.utils.selenium_manager import SeleniumManager
    from selenium.webdriver.common.by import By

    manager = SeleniumManager(headless=False)
    driver = manager.create_driver()
    try:
        driver.get("https://www.ozon.ru/")
        time.sleep(6)

        # Иконка сообщений в шапке: ищем по aria-label/заголовку
        candidates = []
        for el in driver.find_elements(By.CSS_SELECTOR, "[aria-label], [title]"):
            label = (el.get_attribute("aria-label") or el.get_attribute("title") or "")
            if "сообщ" in label.lower() or "чат" in label.lower():
                candidates.append((label, el))
        print("Кандидаты в шапке:", [c[0] for c in candidates])

        if candidates:
            try:
                candidates[0][1].click()
            except Exception:                                # noqa: BLE001
                driver.execute_script("arguments[0].click();", candidates[0][1])
            time.sleep(8)
        else:
            # Нет иконки — пробуем пункт «Сообщения» из меню кабинета
            driver.get("https://www.ozon.ru/my/main")
            time.sleep(6)
            for el in driver.find_elements(By.XPATH, "//*[contains(text(), 'Сообщения')]"):
                try:
                    el.click()
                except Exception:                            # noqa: BLE001
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(8)
                break

        html = driver.find_element("tag name", "html").get_attribute("outerHTML")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(html, encoding="utf-8")
        print(f"\nURL после клика: {driver.current_url}")
        print(f"HTML сохранён: {OUT} ({len(html)} байт)")
        body = driver.find_element("tag name", "body").text
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        print(f"строк {len(lines)}; первые 60:")
        for line in lines[:60]:
            print("  |", line[:90])
        return 0
    finally:
        try:
            manager.close()
        except Exception:                                    # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
