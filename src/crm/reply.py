"""
Ответ продавцу из дашборда: печатаете текст — он уходит в его чат Ozon.

Механизм тот же, что у массовой рассылки: подключаемся к общему Chrome с
профилем парсера, открываем страницу продавца, жмём «Написать», вводим текст
и отправляем. Отличие от рассылки — одно сообщение по запросу человека, а не
проход по очереди.

Вызывающая сторона (дашборд) обязана держать BROWSER_LOCK: как и сбор,
ответ открывает вкладку в общем браузере.
"""

import logging
import time

logger = logging.getLogger(__name__)


def reply_to_seller(data_dir, store, seller_id: str, text: str) -> dict:
    """Открывает чат с продавцом и отправляет текст. Бросает при неудаче."""
    from ..outreach.sender import (
        ChatClient,
        ChatSelectors,
        NoChatAvailable,
        build_driver,
        focus_tab,
    )

    manager, driver = build_driver(headless=False)
    try:
        selectors = ChatSelectors.load(data_dir / "selectors.json")
        client = ChatClient(driver, selectors, manager=manager)

        # Сначала главная: новая вкладка about:blank — на ней проверка входа
        # всегда «сессии нет», даже когда сессия живая (как у отправки).
        focus_tab(manager)
        driver.get("https://www.ozon.ru/")
        time.sleep(3)
        if not client.is_logged_in():
            raise RuntimeError("В браузере нет залогиненной сессии Ozon — выполните вход")

        try:
            client.open_chat(seller_id)
        except NoChatAvailable as exc:
            raise RuntimeError(f"Чат недоступен: {exc}")

        client.send(text)
        if not client.confirm_sent():
            raise RuntimeError("Отправка не подтвердилась — текст остался в поле")

        store.log_message(seller_id, text, "sent")
        logger.info(f"Ответ из дашборда ушёл продавцу {seller_id}")
        return {"ok": True, "seller_id": seller_id}
    finally:
        # Закрываем только свою вкладку, не весь Chrome (там живёт сессия).
        try:
            manager.close()
        except Exception:                                    # noqa: BLE001
            pass
