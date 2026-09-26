"""Ozon-задача воркера: обёртка над существующим ChinaSellerHarvester.

Логика сбора не дублируется — тут только сборка ингредиентов, как в
run_harvest.py cmd_harvest: браузер → клиент страницы → харвестер. Отличие
одно: прогресс уходит не в print, а в стейт-файл воркера.
"""

import logging

from ..crm.harvest import DEFAULT_DELAY, ChinaSellerHarvester, OzonPageClient
from ..crm.store import SellerStore
from .runtime import data_dir

logger = logging.getLogger(__name__)

# Тот же раздел, что и в run_harvest.py: витрина «Товары из Китая».
DEFAULT_SECTION = "https://www.ozon.ru/highlight/tovary-iz-kitaya-935133/"


def run(runtime, args):
    """Прогон сбора Ozon. runtime — WorkerRuntime, args — аргументы CLI."""
    from ..utils.browser import create_browser_manager

    store = SellerStore(data_dir() / "sellers.sqlite3")

    manager = None
    try:
        manager = create_browser_manager(headless=True)
        driver = manager.create_driver()
        # Один раз открываем Ozon: дальше клиент ходит fetch'ем из страницы.
        driver.get("https://www.ozon.ru/")

        # Явный --delay означает фиксированный темп (пользователь выбрал сам).
        adaptive = args.delay == DEFAULT_DELAY
        client = OzonPageClient(driver, delay=args.delay, adaptive=adaptive)

        harvester = ChinaSellerHarvester(
            client=client,
            store=store,
            on_progress=runtime.log,
            progress_cb=lambda done, total, phase: runtime.update(
                done=done, total=total, phase=phase
            ),
            stop_event=runtime.stop_event,
            refresh_days=args.refresh_days,
        )
        stats = harvester.run(
            args.url or DEFAULT_SECTION, pages=args.pages, start_page=args.start_page
        )
        runtime.update_from_stats(stats)
        return stats
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:                            # noqa: BLE001
                logger.warning("Не удалось закрыть браузер воркера", exc_info=True)
