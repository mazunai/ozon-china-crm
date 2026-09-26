"""Сбор китайских продавцов Wildberries: выдача поиска → реквизиты → база.

Конsciously повторяет контракт ChinaSellerHarvester (те же колбэки, тот же
HarvestStats, батчи по 25, пропуск свежих по refresh_days) — дашборд и воркер
не должны знать, чей харвестер под ними работает.

Пайплайн:
    1. выдача поиска (query, страница, limit=100) — supplierId приходит
       прямо в товаре, отдельные запросы карточек не нужны;
    2. группировка товаров по поставщикам;
    3. реквизиты поставщика (seller-info) — единственный источник страны;
    4. классификация: российское юрлицо перебивает «китайские» маркеры
       (тот же урок, что с витриной Ozon — китайский товар ≠ китайский продавец);
    5. подтверждённые китайцы → SellerMetrics → база со статусом new.

v1 ходит только по поисковым запросам: URL категорий WB требуют shard-параметров
каталожного API, это отдельная работа. Ссылка на поиск (.aspx?search=...) тоже
принимается — из неё достаётся текст запроса.
"""

import json
import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import unquote_plus

from ...outreach.scoring import SellerMetrics, score_all
from ..chinese_filter import classify_seller, ru_inn_is_valid
from .transport import WBHttpClient, wb_pace

logger = logging.getLogger(__name__)

# Аналог раздела «Товары из Китая» на Ozon: такого раздела на WB нет,
# поэтому дефолт — поиск по той же смысловой нише.
DEFAULT_QUERY = "товары из китая"

# Размер батча записи в базу: падение в середине прогона не должно
# обнулять работу (тот же принцип, что в Ozon-харвестере).
BATCH_SIZE = 25


def query_from_source(source: str) -> str:
    """Из ссылки на поиск WB или голого текста — текст поискового запроса."""
    source = (source or "").strip()
    if not source:
        return DEFAULT_QUERY

    match = re.search(r"[?&](?:search|query|text)=([^&\s]+)", source)
    if match:
        return unquote_plus(match.group(1))

    if "wildberries.ru" in source or source.startswith("/"):
        raise ValueError(
            "WB v1 понимает поисковые запросы (например «чехол iphone») "
            "или ссылку на поиск wildberries.ru/catalog/0/search.aspx?search=... "
            "Ссылки на категории пока не поддерживаются."
        )
    return source


def extract_products(payload: dict) -> List[dict]:
    """Список товаров из ответа поиска (форма ответа менялась между версиями)."""
    data = payload.get("data")
    if isinstance(data, dict):
        products = data.get("products")
        if isinstance(products, list):
            return products
    products = payload.get("products")
    return products if isinstance(products, list) else []


class WBSellerHarvester:
    """Обходит выдачу WB и собирает подтверждённых китайских поставщиков."""

    def __init__(
        self,
        client: WBHttpClient,
        store=None,
        on_progress: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        stop_event: Optional["threading.Event"] = None,
        refresh_days: int = 7,
        pacing: Optional[float] = None,
    ):
        self.client = client
        self.store = store
        self.on_progress = on_progress
        self.progress_cb = progress_cb
        self.stop_event = stop_event
        self.refresh_days = refresh_days
        # Пауза по умолчанию — из транспорта (WB_PACE), тесты подменяют нулём.
        self.pacing = wb_pace() if pacing is None else pacing
        from ...crm.harvest import HarvestStats

        self.stats = HarvestStats()
        self._known: Dict[str, dict] = {}

    # --- Общие helpers (зеркало Ozon-харвестера) --------------------------

    def cancelled(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _report(self, done: int, total: int, phase: str) -> None:
        if not self.progress_cb:
            return
        try:
            self.progress_cb(done, total, phase)
        except Exception as exc:                         # noqa: BLE001
            logger.debug(f"progress_cb упал: {exc}")

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.on_progress:
            self.on_progress(message)

    def _pause(self) -> None:
        if self.pacing > 0:
            time.sleep(self.pacing)

    # --- Пайплайн ----------------------------------------------------------

    def collect_suppliers(self, query: str, pages: int, start_page: int = 1) -> Dict[str, dict]:
        """Выдача поиска → поставщики: supplier_id → {products, names}."""
        suppliers: Dict[str, dict] = {}
        for page_number in range(start_page, start_page + max(1, pages)):
            if self.cancelled():
                self.stats.cancelled = True
                self._log(f"Отменено на странице {page_number}")
                break

            payload = self.client.search(query, page=page_number, limit=100)
            items = extract_products(payload)
            self.stats.pages += 1

            new = 0
            for item in items:
                nm = str(item.get("id") or "")
                supplier_id = str(item.get("supplierId") or "")
                # Товар без поставщика бесполезен для задачи — пропускаем тихо.
                if not nm or not supplier_id or supplier_id == "0":
                    continue
                record = suppliers.setdefault(supplier_id, {"products": [], "names": []})
                if nm not in record["products"]:
                    record["products"].append(nm)
                    new += 1
                record["names"].append(str(item.get("name") or ""))

            self.stats.products += len(items)
            self._log(f"Страница {page_number}: товаров {len(items)}, новых связок {new}")
            self._report(page_number, pages, "Собираю выдачу")
            self._pause()
            if len(items) < 100:
                self._log("Выдача кончилась")
                break
        return suppliers

    def _supplier_metrics(self, supplier_id: str, record: dict) -> Optional[SellerMetrics]:
        """Реквизиты + классификация → SellerMetrics. None — продавец не китайский."""
        info = self.client.supplier_info(supplier_id)
        if not isinstance(info, dict):
            self.stats.errors.append(
                f"продавец {supplier_id}: реквизиты недоступны "
                "(seller-info не отвечает — проверьте актуальность эндпоинта)"
            )
            return None

        company = str(info.get("supplierName") or info.get("name") or "")
        trademark = str(info.get("trademark") or "")
        inn = str(info.get("INN") or info.get("inn") or "").strip()
        ogrn = str(info.get("ogrn") or "").strip()
        address = str(info.get("address") or "")
        reg_number = inn or ogrn

        # Блок реквизитов склеиваем в формате модалки Ozon (строки через <br>),
        # чтобы классификатор и парсер реквизитов работали без изменений:
        # компания / адрес / регистрационный номер.
        registration_block = "<br>".join(
            x for x in (company, address, reg_number) if x
        )
        cls = classify_seller(
            company_name=company,
            shop_name=trademark,
            inn=reg_number,
            registration_block=registration_block,
        )

        if cls.is_russian or (reg_number and ru_inn_is_valid(inn)):
            self.stats.sellers_rejected += 1
            logger.info(f"Поставщик {supplier_id}: российское юрлицо ({cls.registration_kind})")
            return None
        if not cls.is_chinese:
            self.stats.sellers_rejected += 1
            return None

        feedbacks = info.get("feedbackCount") or 0
        rating = info.get("valuation") or info.get("rating") or 0
        products = record.get("products") or []
        try:
            rating_value = float(rating)
        except (TypeError, ValueError):
            rating_value = 0.0

        return SellerMetrics(
            seller_id=supplier_id,
            shop_name=trademark or company,
            company_name=company,
            orders=0,                       # WB не отдаёт число заказов поставщика
            reviews=int(feedbacks) if str(feedbacks).isdigit() else 0,
            months_on_ozon=0,               # колонка общая для CRM; WB дату старта не отдаёт
            rating=rating_value,
            is_chinese=True,
            province=cls.province,
            uscc=cls.uscc,
            sample_product_url=(
                f"https://www.wildberries.ru/catalog/{products[0]}/detail.aspx"
                if products else ""
            ),
            seller_profile_url=f"https://www.wildberries.ru/seller/{supplier_id}",
            verified_by="uscc" if cls.uscc else "heuristic",
            registration_kind=cls.registration_kind,
            chinese_reasons="; ".join(cls.reasons),
        )

    def run(self, source: str, pages: int = 10, start_page: int = 1):
        """Полный проход: запрос → выдача → реквизиты → база."""
        from ...crm.harvest import HarvestStats  # noqa: F401  (тип результата)

        query = query_from_source(source)
        self._log(f"Поисковый запрос: «{query}»"
                  + (f", начинаю со страницы {start_page}" if start_page > 1 else ""))

        suppliers = self.collect_suppliers(query, pages, start_page)
        self.stats.sellers_found = len(suppliers)
        if not suppliers:
            self._log("Поставщики не найдены — проверьте запрос")
            return self.stats

        self._known = self.store.sellers_meta() if self.store is not None else {}
        self._log(f"Найдено поставщиков: {len(suppliers)}. Проверяю реквизиты…")

        buffer: List[SellerMetrics] = []
        seen: list = []
        pairs: list = []
        added_total = 0

        def flush(force: bool = False) -> None:
            nonlocal buffer, added_total
            if self.store is None or (not force and len(buffer) < BATCH_SIZE):
                return
            if not buffer:
                return
            score_all(buffer)
            added_total += self.store.add_sellers(buffer)
            buffer = []

        for index, (supplier_id, record) in enumerate(suppliers.items(), 1):
            if self.cancelled():
                self.stats.cancelled = True
                self._log(f"Отменено на поставщике {index} из {len(suppliers)}")
                break

            known = self._known.get(supplier_id)
            if known and self._metrics_fresh(known):
                self.stats.sellers_skipped += 1
            else:
                metrics = self._supplier_metrics(supplier_id, record)
                if metrics is not None:
                    buffer.append(metrics)
                self._pause()
            flush()

            if record["products"]:
                nm = record["products"][0]
                seen.append((supplier_id,
                             f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"))
                pairs.extend((nm, supplier_id) for nm in record["products"])
            self._report(index, len(suppliers), "Проверяю реквизиты")
            if index % 10 == 0:
                self._log(f"Проверено поставщиков: {index}/{len(suppliers)}")

        flush(force=True)

        if self.store is not None and not self.cancelled():
            if seen:
                self.store.mark_seen(seen)
            if pairs:
                fresh = self.store.touch_products(pairs)
                self._log(f"Кэш товаров: {len(pairs)} связок (новых {fresh})")
            parts = [f"добавлено {added_total}"]
            refreshed = len(suppliers) - added_total - self.stats.sellers_skipped
            if refreshed > 0:
                parts.append(f"обновлено {refreshed}")
            if self.stats.sellers_skipped:
                parts.append(f"свежих пропущено {self.stats.sellers_skipped}")
            self._log(f"В базу: {', '.join(parts)}")

        self.stats.requests = self.client.requests
        self._log(f"Готово. {self.stats.as_text()}")
        return self.stats

    def _metrics_fresh(self, known: dict) -> bool:
        """Метрики свежие — реквизиты не перечитываем (экономия запросов)."""
        if self.refresh_days <= 0:
            return False
        from datetime import datetime, timedelta, timezone

        raw = known.get("metrics_at") or ""
        if not raw:
            return False
        try:
            metrics_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if metrics_at.tzinfo is None:
            metrics_at = metrics_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - metrics_at
        return age < timedelta(days=self.refresh_days)
