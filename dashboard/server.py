#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-дашборд Ozon-парсера.

Поднимает локальный (или серверный) HTTP-сервер, через который можно:
  • запускать и останавливать парсинг;
  • смотреть живой лог и прогресс (Server-Sent Events);
  • смотреть результаты и фильтровать только китайских продавцов;
  • выгружать CSV.

Запуск:  python3 dashboard/server.py [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DASH_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASH_DIR.parent
WEB_DIR = DASH_DIR / "web"
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_PORT = 8000

# База продавцов: мини-CRM с заметками и статусами
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "sellers.sqlite3"

# Учётные данные дашборда. Дашборд публикуется наружу через SSH-туннель,
# поэтому без пароля он открыт всему интернету. Пароль лежит в .env
# (файл в .gitignore), логин по умолчанию — admin.
ENV_PATH = REPO_ROOT / ".env"


def load_credentials() -> tuple:
    """Читает логин и пароль из окружения или из .env.

    Пароль обязателен при публикации наружу: дашборд отдаёт бизнес-данные, а
    SSH-туннель выставляет его в интернет. Для локальной отладки авторизацию
    можно снять явным флагом OZON_NO_AUTH=1 — «пустая переменная» для этого не
    годится, потому что тогда подставляется значение из .env.
    """
    import os

    if os.environ.get("OZON_NO_AUTH") == "1":
        return "admin", ""

    user = os.environ.get("OZON_DASHBOARD_USER", "")
    password = os.environ.get("OZON_DASHBOARD_PASSWORD", "")

    if (not user or not password) and ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "OZON_DASHBOARD_USER" and not user:
                    user = value.strip()
                elif key.strip() == "OZON_DASHBOARD_PASSWORD" and not password:
                    password = value.strip()
        except Exception:                                  # noqa: BLE001
            pass

    return user or "admin", password


# Поля, которые попадут в Excel-отчёт (китайские признаки — в первую очередь)
EXPORT_FIELDS = [
    "name", "company_name", "inn", "is_chinese", "chinese_confidence",
    "seller_country", "chinese_reasons", "price", "card_price",
    "product_url", "image_url", "orders_count", "reviews_count",
    "average_rating", "working_time",
]


# --------------------------------------------------------------------------- #
#  Доступ к базе продавцов (мини-CRM)
# --------------------------------------------------------------------------- #
class SellersApi:
    """Читает и правит базу продавцов для дашборда.

    Вся работа с SQLite инкапсулирована в `SellerStore`: дашборд не держит
    результаты в памяти, поэтому перезапуск сервера больше ничего не теряет.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._store = None
        self._lock = threading.Lock()

    def store(self):
        with self._lock:
            if self._store is None:
                from src.crm.store import SellerStore
                self._store = SellerStore(self.db_path)
            return self._store

    def sellers(self, query: dict) -> dict:
        """Список продавцов с фильтрами и пагинацией."""
        from src.crm.store import STATUS_LABELS, VERIFY_LABELS

        try:
            limit = min(max(int(query.get("limit", "100")), 1), 1000)
            offset = max(int(query.get("offset", "0")), 0)
        except (TypeError, ValueError):
            limit, offset = 100, 0

        # Фильтры приходят списками через запятую: в интерфейсе их можно
        # выбирать по нескольку сразу (провинции, статусы).
        def multi(name):
            raw = query.get(name, "").strip()
            return [x for x in (part.strip() for part in raw.split(",")) if x] or None

        search = query.get("search", "")
        order_by = query.get("order_by", "score")
        descending = query.get("order", "desc").lower() != "asc"
        statuses = multi("status")
        provinces = multi("province")
        tiers = multi("tier")
        verified_only = query.get("verified_only", "1") not in ("0", "false", "no")

        store = self.store()
        kwargs = dict(statuses=statuses, provinces=provinces, search=search, tiers=tiers)
        rows = store.list_sellers(
            verified_only=verified_only,
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
            **kwargs,
        )
        total = store.count_sellers(verified_only=verified_only, **kwargs)
        companies = store.same_company_counts()

        payload = []
        for row in rows:
            item = dict(row)
            # Сколько ещё витрин у той же компании — подсказка в интерфейсе
            item["same_company"] = companies.get(item.get("uscc") or "", 1)
            payload.append(item)

        return {
            "rows": payload,
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_labels": STATUS_LABELS,
            "verify_labels": VERIFY_LABELS,
            "provinces": store.province_facets(),
        }

    def export_csv(self, query: dict) -> str:
        """Выгрузка продавцов в CSV с учётом активных фильтров."""
        import csv
        import io

        def multi(name):
            raw = query.get(name, "").strip()
            return [x for x in (part.strip() for part in raw.split(",")) if x] or None

        store = self.store()
        rows = store.list_sellers(
            verified_only=query.get("verified_only", "1") not in ("0", "false", "no"),
            statuses=multi("status"),
            provinces=multi("province"),
            tiers=multi("tier"),
            search=query.get("search", ""),
            order_by=query.get("order_by", "score"),
            descending=query.get("order", "desc").lower() != "asc",
            limit=1_000_000,
            offset=0,
        )

        columns = [
            ("seller_id", "seller_id"), ("shop_name", "Магазин"),
            ("company_name", "Компания"), ("uscc", "USCC"),
            ("province", "Провинция"), ("verified_by", "Достоверность"),
            ("orders", "Заказов"), ("reviews", "Отзывов"),
            ("months_on_ozon", "Месяцев на Ozon"), ("rating", "Рейтинг"),
            ("score", "Оценка"), ("status", "Статус"), ("notes", "Заметка"),
            ("seller_profile_url", "Профиль продавца"),
            ("sample_product_url", "Товар"),
        ]
        # BOM и точка с запятой: файл открывают в Excel с русской локалью
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow([title for _, title in columns])
        for row in rows:
            writer.writerow([row[key] for key, _ in columns])
        return "\ufeff" + buffer.getvalue()

    def detail(self, seller_id: str) -> dict:
        """Всё по одному продавцу для карточки."""
        store = self.store()
        row = store.get_seller(seller_id)
        if not row:
            return {}
        item = dict(row)
        companies = store.same_company_counts()
        item["same_company"] = companies.get(item.get("uscc") or "", 1)
        return item

    def summary(self) -> dict:
        """Сводка для шапки: сколько чего, когда собирали, когда бэкап."""
        return self.store().recent_stats()

    def sellers_meta(self) -> dict:
        """Справочники для интерфейса: статусы и уровни достоверности."""
        from src.crm.store import STATUS_LABELS, VERIFY_LABELS
        return {"status_labels": STATUS_LABELS, "verify_labels": VERIFY_LABELS}

    def set_note(self, seller_id: str, note: str) -> dict:
        return {"ok": self.store().set_note(seller_id, note)}

    def set_status(self, seller_id: str, status: str) -> dict:
        return {"ok": self.store().set_status(seller_id, status)}

    def stats(self) -> dict:
        return self.store().stats().as_text()


# Задержка между запросами по умолчанию — как в харвестере
try:
    from src.crm.harvest import DEFAULT_DELAY
except Exception:                                            # noqa: BLE001
    DEFAULT_DELAY = 0.7

CRUD = SellersApi(DB_PATH)
AUTH = load_credentials()

# Раздел Ozon Global, откуда берутся китайские продавцы. Подставляется в форму
# как значение по умолчанию — это и есть «та самая ссылка».
CHINA_SECTION_URL = "https://www.ozon.ru/highlight/tovary-iz-kitaya-935133/"

# Замок на браузерную сессию. Все задачи, которые лезут в Chrome, делят один
# профиль и один порт отладки: если запустить две одновременно, они начнут
# мешать друг другу — переключать вкладки и закрывать их друг у друга. Раньше
# проверки не было вообще, и «Запустить парсинг» + «Собрать» спокойно работали
# вдвоём.
BROWSER_LOCK = threading.Lock()


class HarvestRunner:
    """Запуск сбора китайских продавцов прямо из дашборда.

    Отдельно от `Service`: тот управляет старым парсером категорий (обход
    страницы со скроллом), а здесь — обход раздела Ozon Global по страницам с
    подтверждением страны по USCC. Это разные вещи, и смешивать их не нужно.

    Работает в фоновом потоке, прогресс отдаёт через общую шину событий, чтобы
    интерфейс рисовал его теми же компонентами, что и лог парсера.
    """

    def __init__(self):
        self.state = {
            "running": False,
            "url": "",
            "pages": 0,
            "phase": "",
            "progress": 0,
            "error": "",
            "stats": {},
            "started_at": None,
            "finished_at": None,
        }
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._holds_browser = False

    def status(self) -> dict:
        return dict(self.state)

    def start(self, url: str, pages: int, delay: Optional[float] = None) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Сбор уже идёт")

            url = (url or "").strip() or CHINA_SECTION_URL
            if "ozon.ru" not in url:
                raise ValueError("Укажите ссылку на раздел Ozon")
            pages = max(1, min(int(pages or 10), 200))

            # Занимаем браузер, пока сбор не закончится
            if not BROWSER_LOCK.acquire(blocking=False):
                raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
            self._holds_browser = True

            self._stop.clear()
            self._delay = delay
            self.state.update({
                "running": True, "url": url, "pages": pages,
                "phase": "Запускаю браузер", "progress": 0,
                "error": "", "stats": {},
                "started_at": time.time(), "finished_at": None,
            })

        self._thread = threading.Thread(
            target=self._run, args=(url, pages, getattr(self, "_delay", None)), daemon=True
        )
        self._thread.start()

    def _release_browser(self) -> None:
        if getattr(self, "_holds_browser", False):
            try:
                BROWSER_LOCK.release()
            except RuntimeError:
                pass
            self._holds_browser = False

    def stop(self) -> None:
        """Просит сбор остановиться. Реально работает: харвестер проверяет
        флаг в каждом цикле и выходит на ближайшей итерации."""
        self._stop.set()
        self.state["phase"] = "Останавливаю…"
        HUB.publish({"type": "log", "line": "Запрошена остановка сбора…"})
        HUB.publish({"type": "harvest", **self.state})

    def _release_browser(self) -> None:
        if self._holds_browser:
            try:
                BROWSER_LOCK.release()
            except RuntimeError:
                pass
            self._holds_browser = False

    def _run(self, url: str, pages: int, delay: Optional[float] = None) -> None:
        def log(message: str) -> None:
            HUB.publish({"type": "log", "line": message})

        def progress(done: int, total: int, phase: str) -> None:
            # Три фазы разной длины: сбор товаров, определение продавцов,
            # проверка реквизитов. В одну шкалу их сводим с весами.
            weights = {"Собираю товары": (0, 40), "Определяю продавцов": (40, 35),
                       "Проверяю реквизиты": (75, 25)}
            start, span = weights.get(phase, (0, 0))
            value = start + (span * done / total if total else 0)
            self.state["phase"] = phase
            self.state["progress"] = round(min(value, 100), 1)
            HUB.publish({"type": "harvest", **self.state})

        try:
            from src.crm.harvest import ChinaSellerHarvester, OzonPageClient
            from src.crm.store import SellerStore
            from src.utils.browser import create_browser_manager

            log(f"Раздел: {url}")
            log(f"Страниц: {pages} (по 8 товаров)")

            manager = create_browser_manager(headless=True)
            driver = manager.create_driver()
            try:
                client = OzonPageClient(
                    driver, delay=delay if delay is not None else DEFAULT_DELAY
                )
                client.ensure_ready()
                harvester = ChinaSellerHarvester(
                    client=client,
                    store=SellerStore(DB_PATH),
                    on_progress=log,
                    progress_cb=progress,
                    stop_event=self._stop,
                )
                stats = harvester.run(url, pages=pages)
            finally:
                try:
                    manager.close()
                except Exception:                            # noqa: BLE001
                    pass

            self._release_browser()
            self.state.update({
                "running": False,
                "phase": "Остановлено" if stats.cancelled else "Готово",
                "progress": 100,
                "stats": {
                    "pages": stats.pages, "products": stats.products,
                    "sellers": stats.sellers_found,
                    "uscc": stats.sellers_verified_uscc,
                    "warehouse": stats.sellers_verified_warehouse,
                    "rejected": stats.sellers_rejected,
                    "requests": stats.requests,
                    "cancelled": bool(stats.cancelled),
                    "errors": list(stats.errors)[:20],
                },
                "finished_at": time.time(),
            })
            if stats.cancelled:
                log(
                    f"Сбор остановлен по запросу: страниц {stats.pages}, "
                    f"товаров {stats.products}, продавцов проверено {stats.sellers_found}"
                )
            else:
                log(
                    f"Сбор завершён: страниц {stats.pages}, товаров {stats.products}, "
                    f"продавцов {stats.sellers_found} "
                    f"(по USCC {stats.sellers_verified_uscc}, по складу {stats.sellers_verified_warehouse})"
                )
            HUB.publish({"type": "harvest", **self.state})
            HUB.publish({"type": "harvest_done", "stats": self.state["stats"]})
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Сбор не удался")
            self._release_browser()
            self.state.update({
                "running": False, "phase": "Ошибка", "error": str(exc)[:400],
                "finished_at": time.time(),
            })
            log(f"Ошибка сбора: {exc}")
            HUB.publish({"type": "harvest", **self.state})
            HUB.publish({"type": "harvest_done", "stats": {}, "error": str(exc)[:400]})


HARVEST = HarvestRunner()


# --------------------------------------------------------------------------- #
#  Шина событий: лог и статус уходят подписчикам SSE
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=500)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=3000)
        with self._lock:
            self._subs.add(q)
        for item in list(self.history):          # отдаём историю лога новому клиенту
            try:
                q.put_nowait(item)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        if event.get("type") == "log":
            self.history.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


HUB = Hub()


# --------------------------------------------------------------------------- #
#  Оценка прогресса по строкам лога
# --------------------------------------------------------------------------- #
class Progress:
    """Парсер не сообщает прогресс напрямую, поэтому выводим его из лога."""

    def __init__(self):
        self.phase = "Ожидание"
        self.value = 0
        self.reset()

    def reset(self) -> None:
        self.phase = "Ожидание"
        self.value = 0
        self.products_total = 0
        self.products_done = 0
        self._last_emit = 0.0

    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.7:
            return
        self._last_emit = now
        HUB.publish({"type": "progress", "phase": self.phase, "value": round(self.value, 1)})

    def feed(self, line: str) -> None:
        m = re.search(r"Скролл \d+: \+\d+, всего (\d+)/(\d+)", line)
        if m:
            cur, total = int(m.group(1)), max(int(m.group(2)), 1)
            self.phase = "Сбор ссылок на товары"
            self.value = 5 + min(cur / total, 1.0) * 25
            return self._emit()

        m = re.search(r"Начало парсинга (\d+) товаров", line)
        if m:
            self.products_total = int(m.group(1))
            self.products_done = 0
            self.phase = "Парсинг товаров"
            self.value = 35
            return self._emit(force=True)

        if "обработан успешно" in line and "Товар" in line:
            self.products_done += 1
            if self.products_total:
                share = min(self.products_done / self.products_total, 1.0)
                self.value = 35 + share * 28
            return self._emit()

        if "Фильтр CHINA_ONLY" in line:
            self.phase = "Фильтр: только китайские продавцы"
            self.value = 66
            return self._emit(force=True)

        if "Начинаем парсинг" in line and "продавцов" in line:
            self.phase = "Парсинг продавцов"
            self.value = 74
            return self._emit(force=True)

        if "Продавец" in line and "обработан успешно" in line:
            self.value = min(self.value + 1.5, 95)
            return self._emit()

        if "Excel файл сохранен" in line:
            self.phase = "Экспорт отчёта"
            self.value = 98
            return self._emit(force=True)

        if "Не удалось собрать ссылки товаров" in line:
            self.phase = "Не удалось собрать ссылки"
            return self._emit(force=True)


PROGRESS = Progress()


class HubLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        HUB.publish({
            "type": "log",
            "level": record.levelname,
            "line": line,
            "ts": time.time(),
        })
        PROGRESS.feed(line)


# --------------------------------------------------------------------------- #
#  Сервис: обёртка над AppManager
# --------------------------------------------------------------------------- #
class Service:
    def __init__(self):
        self.manager = None
        self._manager_error: str = ""
        self._watch_thread = None
        self._lock = threading.Lock()
        self._holds_browser = False
        self.state = {
            "running": False,
            "phase": "Ожидание",
            "progress": 0,
            "error": "",
            "category_url": "",
            "max_products": 50,
            "china_only": True,
            "started_at": None,
            "finished_at": None,
            "parser_ready": None,     # None = ещё не проверяли
            "parser_error": "",
        }
        self._results_cache = {"products": [], "stats": {}}

    # --- доступ к парсеру -------------------------------------------------- #
    def get_manager(self):
        if self.manager is not None:
            return self.manager
        if self._manager_error:
            raise RuntimeError(self._manager_error)
        try:
            from src.config.settings import Settings
            from src.core.app_manager import AppManager
            self.manager = AppManager(Settings())
            self.state["parser_ready"] = True
        except Exception as exc:                      # noqa: BLE001
            self._manager_error = (
                f"Парсер недоступен: {exc}. "
                "Установите зависимости: pip3 install -r requirements.txt"
            )
            self.state["parser_ready"] = False
            self.state["parser_error"] = self._manager_error
            logging.getLogger(__name__).error(self._manager_error)
            raise RuntimeError(self._manager_error)
        return self.manager

    def check_parser(self) -> None:
        try:
            self.get_manager()
        except RuntimeError:
            pass

    # --- запуск / остановка ------------------------------------------------ #
    @staticmethod
    def ensure_global(url: str) -> str:
        """Добавляет фильтр «товары из-за рубежа», если его нет в ссылке."""
        if "ozon.ru" in url and "from_global" not in url:
            url += ("&" if "?" in url else "?") + "from_global=true"
        return url

    def start(self, url: str, max_products: int, china_only: bool) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Парсинг уже запущен")
            if not url or "ozon.ru" not in url:
                raise ValueError("Укажите ссылку на категорию или поиск Ozon")

            # Тот же замок, что у сбора: браузер один на всех
            if not BROWSER_LOCK.acquire(blocking=False):
                raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
            self._holds_browser = True

            manager = self.get_manager()
            manager.settings.MAX_PRODUCTS = max(1, int(max_products))
            manager.settings.CHINA_ONLY = bool(china_only)

            final_url = self.ensure_global(url)
            # ВАЖНО: кэш прошлого прогона НЕ обнуляем. Раньше здесь стояло
            # `self._results_cache = {"products": [], "stats": {}}`, и при запуске
            # нового парса таблица пропадала у пользователя. Теперь предыдущие
            # результаты остаются на экране, пока идёт новый обход, а по его
            # завершении заменяются свежими.
            PROGRESS.reset()
            PROGRESS.phase = "Запуск Chrome"
            PROGRESS.value = 3

            ok = manager.start_parsing(final_url, EXPORT_FIELDS)
            if not ok:
                self._release_browser()
                raise ValueError("Парсер уже занят другим заданием")

            self.state.update({
                "running": True,
                "phase": "Запуск",
                "progress": 3,
                "error": "",
                "category_url": final_url,
                "max_products": int(max_products),
                "china_only": bool(china_only),
                "started_at": time.time(),
                "finished_at": None,
            })
            HUB.publish({"type": "status", **self.status()})
            HUB.publish({"type": "log", "level": "INFO", "ts": time.time(),
                         "line": f"Запуск парсинга: {final_url} (лимит {max_products}, only_china={china_only})"})

            if self._watch_thread is None or not self._watch_thread.is_alive():
                self._watch_thread = threading.Thread(target=self._watch, daemon=True)
                self._watch_thread.start()

    def stop(self) -> None:
        if self.manager:
            self.manager.stop_parsing()
            logging.getLogger(__name__).warning("Получена команда остановки парсинга")

    # --- наблюдение за завершением ----------------------------------------- #
    def _watch(self) -> None:
        while True:
            time.sleep(1.0)
            if not self.state["running"] or self.manager is None:
                return
            try:
                alive = bool(self.manager.get_status().get("is_running"))
            except Exception:
                alive = False
            if alive:
                continue

            self.state["running"] = False
            self.state["finished_at"] = time.time()
            self.state["phase"] = "Готово"
            self._release_browser()
            self.state["progress"] = 100
            PROGRESS.phase = "Готово"
            PROGRESS.value = 100
            self._results_cache = self._serialize()
            stats = self._results_cache["stats"]
            self.state["stats"] = stats
            # Автосохранение в базу: иначе результат прогона живёт только до
            # перезапуска дашборда, а при следующем парсе заменяется новым.
            saved = self._persist_sellers()
            if saved:
                stats["saved_to_db"] = saved
            HUB.publish({"type": "done", "stats": stats,
                         "products": self._results_cache["products"]})
            HUB.publish({"type": "status", **self.status()})
            logging.getLogger(__name__).info(
                "Готово: товаров %s, китайских %s, в базу %s",
                stats.get("total"), stats.get("chinese"), saved
            )
            return

    def _persist_sellers(self) -> int:
        """Переносит продавцов из последнего прогона в базу.

        В первую очередь попадают подтверждённые по USCC — реквизиты читает
        seller_parser. Остальные сохраняются как эвристика и в дашборде по
        умолчанию не показываются.
        """
        try:
            from src.crm.store import VERIFY_HEURISTIC, VERIFY_NONE, VERIFY_USCC
            from src.outreach.scoring import SellerMetrics, score_all

            results = (self.manager.last_results if self.manager else None) or {}
            sellers = results.get("sellers") or []
            if not sellers:
                return 0

            by_id = {getattr(s, "seller_id", ""): s for s in sellers if getattr(s, "seller_id", "")}
            links = results.get("links") or {}
            product_by_seller = {}
            for product in results.get("products") or []:
                seller_id = getattr(product, "seller_id", "")
                if seller_id and seller_id not in product_by_seller:
                    url = getattr(product, "seller_link", "") or ""
                    for product_url in links:
                        if getattr(product, "article", "") and product.article in product_url:
                            url = product_url
                            break
                    product_by_seller[seller_id] = url or getattr(product, "seller_link", "")

            metrics = []
            for seller_id, seller in by_id.items():
                uscc = getattr(seller, "uscc", "") or ""
                is_chinese = bool(getattr(seller, "is_chinese", False))
                if uscc:
                    verified = VERIFY_USCC
                elif is_chinese:
                    # Эвристики: в базе хранятся, но в дашборде скрыты по умолчанию
                    verified = VERIFY_HEURISTIC
                else:
                    verified = VERIFY_NONE

                metrics.append(SellerMetrics.from_raw(
                    seller_id=seller_id,
                    orders_count=getattr(seller, "orders_count", ""),
                    reviews_count=getattr(seller, "reviews_count", ""),
                    working_time=getattr(seller, "working_time", ""),
                    average_rating=getattr(seller, "average_rating", ""),
                    shop_name=getattr(seller, "shop_name", "") or "",
                    company_name=getattr(seller, "company_name", ""),
                    is_chinese=bool(uscc) or is_chinese,
                    uscc=uscc,
                    province=getattr(seller, "province", "") or "",
                    registration_kind=getattr(seller, "registration_kind", "unknown") or "unknown",
                    seller_profile_url=f"https://www.ozon.ru/seller/{seller_id}/",
                    sample_product_url=product_by_seller.get(seller_id, ""),
                    verified_by=verified,
                    chinese_reasons=getattr(seller, "chinese_reasons", ""),
                ))

            score_all(metrics)
            return CRUD.store().add_sellers(metrics)
        except Exception as exc:                            # noqa: BLE001
            logging.getLogger(__name__).warning(f"Не удалось сохранить продавцов в базу: {exc}")
            return 0

    # --- результаты --------------------------------------------------------- #
    def _serialize(self) -> dict:
        results = (self.manager.last_results if self.manager else None) or {}
        products = results.get("products") or []
        links = results.get("links") or {}
        sellers = results.get("seller_data") or {}

        rows = []
        for p in products:
            article = getattr(p, "article", "")
            product_url = ""
            for u in links:
                if article and article in u:
                    product_url = u
                    break
            s = sellers.get(getattr(p, "seller_id", ""))
            rows.append({
                "article": article,
                "name": getattr(p, "name", ""),
                "seller": getattr(p, "company_name", ""),
                "company": getattr(s, "company_name", "") if s else "",
                "inn": (getattr(s, "inn", "") if s else "") or getattr(p, "company_inn", ""),
                "country": getattr(p, "seller_country", "") or (getattr(s, "seller_country", "") if s else ""),
                "is_chinese": bool(getattr(p, "is_chinese", False)),
                "confidence": round(float(getattr(p, "chinese_confidence", 0.0) or 0.0), 3),
                "reasons": getattr(p, "chinese_reasons", "") or (getattr(s, "chinese_reasons", "") if s else ""),
                "price": getattr(p, "price", 0),
                "card_price": getattr(p, "card_price", 0),
                "original_price": getattr(p, "original_price", 0),
                "image": getattr(p, "image_url", ""),
                "url": product_url,
                "orders": getattr(s, "orders_count", "") if s else "",
                "rating": getattr(s, "average_rating", "") if s else "",
                "reviews": getattr(s, "reviews_count", "") if s else "",
                "success": bool(getattr(p, "success", False)),
                "error": getattr(p, "error", ""),
            })

        chinese = [r for r in rows if r["is_chinese"]]
        confs = [r["confidence"] for r in chinese]
        elapsed = 0.0
        if self.state["started_at"]:
            end = self.state["finished_at"] or time.time()
            elapsed = end - self.state["started_at"]
        stats = {
            "total": len(rows),
            "chinese": len(chinese),
            "others": len(rows) - len(chinese),
            "failed": len([r for r in rows if not r["success"]]),
            "avg_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
            "elapsed": round(elapsed, 1),
            "output_folder": results.get("output_folder", ""),
        }
        return {"products": rows, "stats": stats}

    def results(self) -> dict:
        if self.state["running"] and not self._results_cache["products"]:
            return {"products": [], "stats": {}}
        if not self._results_cache["products"] and self.manager:
            self._results_cache = self._serialize()
        return self._results_cache

    def status(self) -> dict:
        st = dict(self.state)
        if st["running"]:
            st["phase"] = PROGRESS.phase
            st["progress"] = round(PROGRESS.value, 1)
        return st


SERVICE = Service()


# --------------------------------------------------------------------------- #
#  HTTP-сервер
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OzonParserDashboard/1.0"

    def log_message(self, fmt, *args):        # не засоряем консоль запросами
        pass

    # --- авторизация --------------------------------------------------------- #
    def _check_auth(self) -> bool:
        """HTTP Basic. Возвращает True, если запрос можно обработать.

        Дашборд отдаёт наружу бизнес-данные (заметки, статусы по продавцам),
        поэтому без пароля он не должен быть доступен. Пароль берётся из .env.
        """
        if not AUTH[1]:
            return True
        import base64

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, password = decoded.partition(":")
                if user == AUTH[0] and password == AUTH[1]:
                    return True
            except Exception:                              # noqa: BLE001
                pass

        # Тело — JSON, потому что все клиенты разбирают ответ как JSON. На
        # текстовом теле интерфейс показывал «Ошибка сети: SyntaxError» вместо
        # понятного «нужен вход».
        body = json.dumps({"error": "Требуется вход"}, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ozon CRM"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # --- утилиты ------------------------------------------------------------ #
    def _query(self) -> dict:
        """Query-параметры запроса в виде словаря."""
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        return {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            return self._send_json({"error": f"нет файла {path.name}"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- GET ---------------------------------------------------------------- #
    def do_GET(self):
        try:
            self._do_get()
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Ошибка обработки GET %s", self.path)
            self._send_json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def _do_get(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/sellers":
            return self._send_json(CRUD.sellers(self._query()))
        if path == "/api/sellers/meta":
            return self._send_json(CRUD.sellers_meta())
        if path == "/api/summary":
            return self._send_json(CRUD.summary())
        if path == "/api/seller/detail":
            seller_id = self._query().get("seller_id", "")
            detail = CRUD.detail(seller_id)
            return self._send_json(detail or {"error": "Продавец не найден"},
                                   200 if detail else 404)
        if path == "/api/sellers/export.csv":
            body = CRUD.export_csv(self._query()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="ozon-sellers.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/harvest":
            return self._send_json({
                **HARVEST.status(),
                "default_url": CHINA_SECTION_URL,
            })
        if path == "/api/status":
            return self._send_json({**SERVICE.status(), "harvest": HARVEST.status()})
        if path == "/api/results":
            return self._send_json(SERVICE.results())
        if path == "/api/health":
            return self._send_json({"ok": True})
        if path == "/api/parser-check":
            SERVICE.check_parser()
            return self._send_json({
                "ready": SERVICE.state["parser_ready"],
                "error": SERVICE.state["parser_error"],
            })
        if path == "/api/events":
            return self._serve_events()
        self._send_json({"error": "not found"}, 404)

    def _serve_events(self) -> None:
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    # --- POST --------------------------------------------------------------- #
    def do_POST(self):
        try:
            self._do_post()
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Ошибка обработки POST %s", self.path)
            self._send_json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def _do_post(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._send_json({"error": "Некорректный JSON"}, 400)

        if path == "/api/seller/note":
            seller_id = str(data.get("seller_id", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            return self._send_json(CRUD.set_note(seller_id, str(data.get("note", ""))))

        if path == "/api/seller/status":
            seller_id = str(data.get("seller_id", "")).strip()
            status = str(data.get("status", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            try:
                return self._send_json(CRUD.set_status(seller_id, status))
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/sellers/purge":
            ids = data.get("seller_ids") or []
            if not isinstance(ids, list) or not ids:
                return self._send_json({"error": "Не выбрано ни одной записи"}, 400)
            # Удаляем только неподтверждённых: подтверждённых сносить по этой
            # кнопке нельзя, иначе случайное выделение уничтожит рабочие данные.
            store = CRUD.store()
            safe = [str(x) for x in ids]
            rows = store.list_sellers(limit=1_000_000, tiers=["none", "heuristic"])
            allowed = {r["seller_id"] for r in rows}
            targets = [x for x in safe if x in allowed]
            if not targets:
                return self._send_json({"error": "Среди выбранных нет неподтверждённых"}, 400)
            return self._send_json({"deleted": store.delete_sellers(targets),
                                    "skipped": len(safe) - len(targets)})

        if path == "/api/harvest":
            try:
                delay = data.get("delay")
                HARVEST.start(
                    str(data.get("url", "")).strip(),
                    int(data.get("pages") or 10),
                    delay=float(delay) if delay is not None else None,
                )
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True})

        if path == "/api/harvest/stop":
            HARVEST.stop()
            return self._send_json({"ok": True})

        if path == "/api/start":
            try:
                SERVICE.start(
                    str(data.get("url", "")).strip(),
                    int(data.get("max_products") or 50),
                    bool(data.get("china_only", True)),
                )
            except RuntimeError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 409)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True})

        if path == "/api/stop":
            SERVICE.stop()
            return self._send_json({"ok": True})

        self._send_json({"error": "not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Дашборд Ozon-парсера")
    parser.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт")
    parser.add_argument("--open", action="store_true", help="открыть браузер")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), HubLogHandler()],
    )
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("WDM").setLevel(logging.WARNING)

    is_local = args.host in ("127.0.0.1", "localhost")
    url = f"http://{'127.0.0.1' if is_local else args.host}:{args.port}/"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  Дашборд Ozon-парсера: {url}\n  Остановка: Ctrl+C\n", flush=True)

    SERVICE.check_parser()
    if args.open or is_local:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера...", flush=True)
    finally:
        SERVICE.stop()
        server.server_close()


if __name__ == "__main__":
    main()