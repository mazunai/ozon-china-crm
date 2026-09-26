"""Тесты WB-харвестера: разбор запроса, группировка поставщиков,
классификация (российское юрлицо vs китайские маркеры), запись в базу,
распространение WBBlocked. Без сети — фейковый клиент."""

import tempfile
import unittest
from pathlib import Path

from src.crm.store import SellerStore
from src.parsers.wb.harvest import (
    DEFAULT_QUERY,
    WBSellerHarvester,
    extract_products,
    query_from_source,
)
from src.parsers.wb.transport import WBBlocked


# --- Фейковый клиент: ответы по страницам и поставщикам ----------------------

class FakeWBClient:
    def __init__(self, search_pages=None, suppliers=None):
        self.search_pages = search_pages or {}
        self.suppliers = suppliers or {}
        self.requests = 0
        self.search_calls = []
        self.info_calls = []

    def search(self, query, page=1, limit=100, sort="popular"):
        self.search_calls.append((query, page))
        self.requests += 1
        return self.search_pages.get(page, {"data": {"products": []}})

    def supplier_info(self, supplier_id):
        self.info_calls.append(supplier_id)
        self.requests += 1
        info = self.suppliers.get(supplier_id)
        if isinstance(info, Exception):
            raise info
        return info


RU_SUPPLIER = {
    "supplierName": "ООО Ромашка",
    "trademark": "Ромашка",
    "INN": "7701234567",
    "address": "г. Москва, ул. Ленина, 1",
    "feedbackCount": "12",
    "valuation": 4.9,
}

CN_SUPPLIER = {
    "supplierName": "Yiwu Xinya Trading Co., Ltd.",
    "trademark": "XINYA",
    "INN": "",
    "address": "浙江省义乌市江东街道",
    "feedbackCount": "412",
    "valuation": 4.8,
}


def page_payload(items):
    # Современная форма ответа поиска: {data: {products: [...]}}.
    return {"metadata": {"total": len(items)}, "data": {"products": items}}


class QueryParsingTests(unittest.TestCase):
    def test_plain_text_is_query(self):
        self.assertEqual(query_from_source("чехол iphone 13"), "чехол iphone 13")

    def test_empty_is_default(self):
        self.assertEqual(query_from_source(""), DEFAULT_QUERY)

    def test_search_url_param_extracted_and_decoded(self):
        url = "https://www.wildberries.ru/catalog/0/search.aspx?search=%D0%BD%D0%BE%D1%81%D0%BA%D0%B8&page=2"
        self.assertEqual(query_from_source(url), "носки")

    def test_category_url_rejected_with_clear_error(self):
        with self.assertRaises(ValueError):
            query_from_source("https://www.wildberries.ru/catalog/zhenshinam/odezhda")


class ExtractProductsTests(unittest.TestCase):
    def test_modern_shape(self):
        self.assertEqual(len(extract_products(page_payload([{"id": 1}]))), 1)

    def test_legacy_shape(self):
        self.assertEqual(len(extract_products({"products": [{"id": 1}]})), 1)

    def test_garbage_is_empty(self):
        self.assertEqual(extract_products({"unexpected": 1}), [])


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SellerStore(Path(self.tmp.name) / "sellers-wb.sqlite3")
        self.client = FakeWBClient(
            search_pages={
                1: page_payload([
                    {"id": 1001, "name": "Органайзер", "supplierId": 200},
                    {"id": 1002, "name": "Носки", "supplierId": 100},
                    {"id": 1003, "name": "Без поставщика", "supplierId": 0},
                ]),
            },
            suppliers={"100": RU_SUPPLIER, "200": CN_SUPPLIER, "300": None},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def harvest(self, **kwargs):
        harvester = WBSellerHarvester(
            client=self.client, store=self.store, pacing=0, **kwargs
        )
        return harvester.run("органайзер", pages=1)

    def test_russian_legal_entity_rejected(self):
        stats = self.harvest()
        self.assertEqual(stats.sellers_rejected, 1)          # только ООО Ромашка
        self.assertIsNone(self.store.get_seller("100"))

    def test_chinese_supplier_saved(self):
        stats = self.harvest()
        row = self.store.get_seller("200")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_chinese"]), 1)
        self.assertEqual(row["verified_by"], "heuristic")    # иероглифы, не USCC
        self.assertIn("wildberries.ru/seller/200", row["seller_profile_url"])
        self.assertIn("catalog/1001/detail.aspx", row["sample_product_url"])
        self.assertEqual(stats.sellers_found, 2)             # 0-поставщик отсеян раньше

    def test_supplier_without_info_counts_as_error_not_seller(self):
        self.client.search_pages[1] = page_payload(
            [{"id": 1004, "name": "Кружка", "supplierId": 300}]
        )
        stats = self.harvest()
        self.assertEqual(len(stats.errors), 1)
        self.assertIsNone(self.store.get_seller("300"))

    def test_no_russian_rows_in_ozon_store(self):
        # Данные не смешиваются: харвестер пишет только в свою базу.
        other = SellerStore(Path(self.tmp.name) / "sellers.sqlite3")
        self.harvest()
        self.assertIsNone(other.get_seller("200"))

    def test_fresh_known_supplier_skipped_without_request(self):
        self.harvest()                                       # первый прогон пишет 200
        info_calls_before = list(self.client.info_calls)
        stats = WBSellerHarvester(
            client=self.client, store=self.store, pacing=0, refresh_days=7
        ).run("органайзер", pages=1)
        self.assertEqual(stats.sellers_skipped, 1)
        # Свежий (сохранённый) поставщик не перечитывается; отклонённый
        # российский в базу не попадает, поэтому перепроверяется каждый прогон —
        # так же ведёт себя Ozon-харвестер, кэш отказов в v1 не делаем.
        second_run = self.client.info_calls[len(info_calls_before):]
        self.assertNotIn("200", second_run)
        self.assertIn("100", second_run)

    def test_blocked_exception_propagates(self):
        self.client.suppliers["100"] = WBBlocked("WB закрыл доступ (HTTP 403)")
        with self.assertRaises(WBBlocked):
            self.harvest()

    def test_cancel_via_stop_event(self):
        import threading

        event = threading.Event()
        event.set()
        harvester = WBSellerHarvester(
            client=self.client, store=self.store, pacing=0, stop_event=event
        )
        stats = harvester.run("органайзер", pages=3)
        self.assertTrue(stats.cancelled)
        self.assertEqual(stats.pages, 0)


if __name__ == "__main__":
    unittest.main()
