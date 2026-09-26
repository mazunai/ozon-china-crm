#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа воркера сбора. Запускается дашбордом отдельным процессом:

    python run_worker.py --marketplace ozon --pages 10
    python run_worker.py --marketplace wb --url "носки женские" --pages 5

Воркер сам пишет pid/стейт/стоп-файлы (src/workers/runtime.py) и реагирует на
стоп-файл: дашборд только создаёт файл и читает стейт, никакой магии сокетов.
Ручной запуск тоже работает — удобно для отладки в терминале.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.utils.logger import setup_logging
from src.workers.runtime import MARKETPLACES, WorkerRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Воркер сбора китайских продавцов (Ozon / Wildberries)"
    )
    parser.add_argument("--marketplace", required=True, choices=MARKETPLACES)
    parser.add_argument("--url", default="",
                        help="ссылка на раздел/поиск или поисковый запрос")
    parser.add_argument("--pages", type=int, default=10,
                        help="сколько страниц обойти")
    parser.add_argument("--start-page", type=int, default=1, metavar="N",
                        help="начать со страницы N (докатка больших обходов)")
    parser.add_argument("--delay", type=float, default=None,
                        help="пауза между запросами, сек (по умолчанию — своя у каждого маркетплейса)")
    parser.add_argument("--refresh-days", type=int, default=7, metavar="N",
                        help="перечитывать известных продавцов раз в N дней; 0 — никогда")
    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()

    # Пауза по умолчанию — на стороне задачи маркетплейса: у Ozon и WB разный
    # запас прочности, и одна общая константа тут была бы ложью.
    if args.delay is None:
        args.delay = None  # задача сама подставит свою

    runtime = WorkerRuntime(args.marketplace)
    runtime.attach_file_logging()
    logger = logging.getLogger(__name__)

    try:
        runtime.acquire()
    except RuntimeError as exc:
        logger.error(f"Отказ запуска: {exc}")
        return 2

    status = "error"
    try:
        if args.marketplace == "ozon":
            from src.workers.ozon_harvest import run as run_ozon
            stats = run_ozon(runtime, args)
        else:
            from src.workers.wb_harvest import run as run_wb
            stats = run_wb(runtime, args)

        if stats.cancelled:
            status = "stopped"
            runtime.log("Сбор остановлен по запросу")
        else:
            status = "ok"
            runtime.log(f"Готово. {stats.as_text()}")
        return 0
    except KeyboardInterrupt:
        status = "stopped"
        runtime.log("Прервано пользователем")
        return 130
    except Exception as exc:                             # noqa: BLE001
        logger.exception("Воркер упал")
        runtime.update(last_log=f"Ошибка: {exc}", errors=runtime._state.get("errors", 0) + 1)
        return 1
    finally:
        runtime.finish(status)


if __name__ == "__main__":
    sys.exit(main())
