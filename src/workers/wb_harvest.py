"""WB-задача воркера: база sellers-wb.sqlite3 + клиент без браузера.

База отдельная от Ozon (решение владельца проекта): та же SellerStore, свой
файл. Продавцы WB живут со своей CRM-историей и не пересекаются с Ozon
ни по данным, ни по процессу.
"""

import logging

from ..crm.store import SellerStore
from ..parsers.wb.harvest import DEFAULT_QUERY, WBSellerHarvester
from ..parsers.wb.transport import WBBlocked, WBHttpClient
from .runtime import data_dir

logger = logging.getLogger(__name__)


def run(runtime, args):
    """Прогон сбора WB. runtime — WorkerRuntime, args — аргументы CLI."""
    store = SellerStore(data_dir() / "sellers-wb.sqlite3")
    client = WBHttpClient()
    source = args.url or DEFAULT_QUERY

    harvester = WBSellerHarvester(
        client=client,
        store=store,
        on_progress=runtime.log,
        progress_cb=lambda done, total, phase: runtime.update(
            done=done, total=total, phase=phase
        ),
        stop_event=runtime.stop_event,
        refresh_days=args.refresh_days,
    )
    try:
        stats = harvester.run(source, pages=args.pages, start_page=args.start_page)
        runtime.update_from_stats(stats)
        return stats
    except WBBlocked as exc:
        # Не ошибка кода, а состояние сети: дашборд покажет «нужен прокси».
        runtime.mark_blocked(str(exc))
        raise
