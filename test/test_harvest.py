"""Тесты ускоренного сбора v1.2: адаптивная пауза, инкрементальный сбор,
кэш «товар → продавец», порядок проверки (модалка до склада)."""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.crm.harvest import (
    DELAY_CEILING,
    DELAY_FLOOR,
    ChinaSellerHarvester,
    OzonPageClient,
)
from src.crm.store import VERIFY_USCC, VERIFY_WAREHOUSE, SellerStore


# --- Фейковый драйвер: только то, что нужно клиенту -------------------------

class FakeDriver:
    """Эмуляция Selenium-драйвера для fetch_json через execute_async_script."""

    def __init__(self, response: str, page_source: str = "x" * 30_000):
        self._response = response
        self._page_source = page_source
        self._url = "https://www.ozon.ru/"
        self.slept = 0.0

    def execute_async_script(self, script):          # noqa: ARG002
        return self._response

    @property
    def page_source(self):
        return self._page_source

    @property
    def current_url(self):
        return self._url

    def get(self, url):
        self._url = url


# --- Адаптивная пауза -------------------------------------------------------

class TestAdaptiveDelay(unittest.TestCase):
    def test_starts_at_default(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True, min_delay=0.3)
        self.assertAlmostEqual(client.delay, 0.35)

    def test_bad_response_doubles_delay(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True, min_delay=0.3)
        client._adapt(0.1, healthy=False)
        self.assertAlmostEqual(client.delay, 0.7)
        # 16 чистых ответов = два шага ослабления (штраф 0.7 -> 0.63 -> 0.567)
        for _ in range(16):
            client._adapt(0.1, healthy=True)
        self.assertAlmostEqual(client.delay, 0.7 * 0.9 ** 2, places=6)
        # при долгой чистой серии пауза опускается до пола
        for _ in range(200):
            client._adapt(0.1, healthy=True)
        self.assertAlmostEqual(client.delay, DELAY_FLOOR, places=4)

    def test_clean_streak_eases_delay_to_floor(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True,
                                min_delay=0.2, max_delay=5.0)
        client.delay = 1.0
        for _ in range(8):
            client._adapt(0.1, healthy=True)
        self.assertAlmostEqual(client.delay, 0.9 * 1.0, places=4)
        for _ in range(8):
            client._adapt(0.1, healthy=True)
        self.assertAlmostEqual(client.delay, 0.81, places=4)

    def test_slow_fetch_raises_delay(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True,
                                min_delay=0.2, max_delay=5.0)
        client.delay = 1.0
        client._adapt(elapsed=2.5, healthy=True)     # транспорт по умолчанию fetch
        self.assertAlmostEqual(client.delay, 1.5, places=4)

    def test_navigation_transport_not_punished_for_slow(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True)
        client._transport = "navigate"
        client.delay = 1.0
        client._adapt(elapsed=5.0, healthy=True)
        self.assertEqual(client.delay, 1.0)

    def test_delay_never_exceeds_ceiling(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=True)
        for _ in range(20):
            client._adapt(0.1, healthy=False)
        self.assertLessEqual(client.delay, DELAY_CEILING)

    def test_adaptive_disabled_keeps_delay_fixed(self):
        client = OzonPageClient(FakeDriver("{}"), adaptive=False, delay=2.0)
        client._adapt(0.1, healthy=False)
        client._adapt(0.1, healthy=True)
        self.assertEqual(client.delay, 2.0)

    def test_fetch_json_counts_and_sleeps(self):
        driver = FakeDriver('{"ok": true}')
        client = OzonPageClient(driver, adaptive=False, delay=0.01)
        data = client.fetch_json("/api/test")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(client.requests, 1)

    def test_no_sleep_with_zero_delay(self):
        driver = FakeDriver('{"ok": true}')
        client = OzonPageClient(driver, adaptive=False, delay=0)
        client.fetch_json("/api/test")
        self.assertEqual(client.delay, 0)


# --- Свежесть метрик --------------------------------------------------------

class TestMetricsFresh(unittest.TestCase):
    def _client(self):
        return OzonPageClient(FakeDriver('{"ok": true}'))

    def _harvester(self):
        return ChinaSellerHarvester(self._client(), refresh_days=7)

    def test_fresh_metrics_are_fresh(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        h = self._harvester()
        self.assertTrue(h._metrics_fresh({"metrics_at": now}, 7))

    def test_expired_metrics_need_refresh(self):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        h = self._harvester()
        self.assertFalse(h._metrics_fresh({"metrics_at": old}, 7))

    def test_refresh_days_zero_means_always_fresh(self):
        h = self._harvester()
        self.assertTrue(h._metrics_fresh({"metrics_at": ""}, 0))
        self.assertTrue(h._metrics_fresh({"metrics_at": "2000-01-01"}, 0))

    def test_empty_metrics_need_refresh(self):
        h = self._harvester()
        self.assertFalse(h._metrics_fresh({"metrics_at": ""}, 7))

    def test_garbage_metrics_need_refresh(self):
        h = self._harvester()
        self.assertFalse(h._metrics_fresh({"metrics_at": "не дата"}, 7))


# --- Кэш товар → продавец ---------------------------------------------------

def card_json(seller_id: str, shop_name: str) -> dict:
    return {
        "widgetStates": {
            f"webStickyProducts-{seller_id}": json.dumps({
                "seller": {"name": shop_name, "link": f"https://www.ozon.ru/seller/{seller_id}/"},
            }),
        },
    }


def modal_uscc(uscc: str = "91330782MAEATRWE13") -> dict:
    block = f"Yiwushisenyibaozhuangzhipinyouxiangongsi<br>yiwushi<br>{uscc}"
    return {"widgetStates": {f"textBlock-info-{uscc[-4:]}": json.dumps({"text": block})}}


class FakeClient:
    """Фейковый клиент харвестера: страницы и модалки по словарю, счётчик вызовов."""

    def __init__(self, pages=None, modals=None, warehouse_urls=frozenset()):
        self.pages = pages or {}
        self.modals = modals or {}
        self.warehouse_urls = warehouse_urls
        self.calls = []          # ("page"|"modal"|"fetch_json", ключ)
        self.requests = 0
        self.last_error = ""

    def page(self, path):
        self.calls.append(("page", path))
        self.requests += 1
        return self.pages.get(path)

    def seller_modal(self, seller_id):
        self.calls.append(("modal", seller_id))
        self.requests += 1
        return self.modals.get(seller_id)

    def fetch_json(self, url, method="GET", body=None):    # noqa: ARG002
        self.calls.append(("delivery", url))
        self.requests += 1
        return None


class TestProductCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SellerStore(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _grid(self, ids) -> dict:
        return {
            "layout": [{
                "stateId": "tileGridDesktop-1",
                "component": "tileGrid-1",
                "params": "{}",
            }],
            "widgetStates": {
                "tileGridDesktop-1": json.dumps({"items": [{"id": i} for i in ids]}),
            },
        }

    def _grid_with_recommendations(self, real_ids, rec_ids) -> dict:
        """Страница раздела с ДВУМЯ гридами: настоящий (params="{}") и
        рекомендательный («Возможно, вам понравится» — title в params).
        Главная ловушка проекта: у рекомендаций российские товары."""
        return {
            "layout": [
                {"stateId": "tileGridDesktop-1", "component": "tileGrid-1", "params": "{}"},
                {"stateId": "tileGridDesktop-2", "component": "tileGrid-2",
                 "params": json.dumps({"title": "Возможно, вам понравится"})},
            ],
            "widgetStates": {
                "tileGridDesktop-1": json.dumps({"items": [{"id": i} for i in real_ids]}),
                "tileGridDesktop-2": json.dumps({"items": [{"id": i} for i in rec_ids]}),
            },
        }

    def test_collect_products_rejects_recommendation_grid(self):
        """Товары из рекомендательного грида не попадают в выборку.

        AGENTS.md §3.3.1: грид с title в params — рекомендации с российскими
        товарами. Ослабление этой отбраковки оставит набор зелёным при
        потоке российских продавцов в китайскую базу.
        """
        client = FakeClient(pages={
            SECTION_PATH: self._grid_with_recommendations(
                ["101", "102"], ["900", "901", "902"]),
            f"{SECTION_PATH}?page=2": self._grid([]),
        })
        harvester = ChinaSellerHarvester(client, store=self.store)
        ids = harvester.collect_products(SECTION_PATH, 2)
        self.assertEqual(ids, ["101", "102"])

    def test_collect_products_start_page_jumps(self):
        """start_page не перечитывает предыдущие страницы раздела."""
        client = FakeClient(pages={
            SECTION_PATH: self._grid(["1", "2"]),
            f"{SECTION_PATH}?page=2": self._grid(["3", "4"]),
            f"{SECTION_PATH}?page=3": self._grid(["5", "6"]),
            f"{SECTION_PATH}?page=4": self._grid(["7", "8"]),
        })
        harvester = ChinaSellerHarvester(client, store=self.store)

        # со страницы 3: страницы 1-2 не запрашиваются вообще
        ids = harvester.collect_products(SECTION_PATH, 2, start_page=3)
        self.assertEqual(ids, ["5", "6", "7", "8"])
        fetched = [key for kind, key in client.calls if kind == "page"]
        self.assertEqual(fetched, [f"{SECTION_PATH}?page=3", f"{SECTION_PATH}?page=4"])

        # первая страница ходит по голому пути (без ?page=), как и раньше
        client2 = FakeClient(pages={SECTION_PATH: self._grid(["1"])})
        harvester2 = ChinaSellerHarvester(client2, store=self.store)
        ids2 = harvester2.collect_products(SECTION_PATH, 1)
        self.assertEqual(ids2, ["1"])
        self.assertEqual(client2.calls, [("page", SECTION_PATH)])

    def _seed(self):
        """Два известных продавца + запись в кэше товаров."""
        from src.outreach.scoring import SellerMetrics

        self.store.add_sellers([
            SellerMetrics(
                seller_id="1000001", shop_name="Магазин А",
                verified_by=VERIFY_USCC, is_chinese=True,
                sample_product_url="https://www.ozon.ru/product/1/",
            ),
            SellerMetrics(
                seller_id="1000002", shop_name="Магазин Б",
                verified_by=VERIFY_USCC, is_chinese=True,
                sample_product_url="https://www.ozon.ru/product/2/",
            ),
        ])
        self.store.touch_products([("ART_CACHED", "1000001")])

    def test_cached_article_skips_card_fetch(self):
        self._seed()
        client = FakeClient(pages={"/product/ART_NEW/": card_json("1000002", "Магазин Б")})
        harvester = ChinaSellerHarvester(client, store=self.store)
        harvester._known = self.store.sellers_meta()

        sellers = harvester.resolve_sellers(["ART_CACHED", "ART_NEW"])

        # Карточку качали только для нового товара
        fetched = [key for kind, key in client.calls if kind == "page"]
        self.assertEqual(fetched, ["/product/ART_NEW/"])
        self.assertEqual(harvester.stats.cards_cached, 1)
        self.assertEqual(harvester.stats.products, 1)
        # Кэшированный продавец получил товар, название — из базы
        self.assertEqual(sellers["1000001"]["products"], ["ART_CACHED"])
        self.assertEqual(sellers["1000001"]["shop_name"], "Магазин А")

    def test_cached_seller_deleted_from_db_refetches_card(self):
        self._seed()
        # Продавца из кэша удалили — кэш не должен сработать
        self.store.delete_sellers(["1000001"])
        client = FakeClient(pages={"/product/ART_CACHED/": card_json("1000001", "Магазин А")})
        harvester = ChinaSellerHarvester(client, store=self.store)
        harvester._known = self.store.sellers_meta()

        sellers = harvester.resolve_sellers(["ART_CACHED"])
        self.assertEqual(harvester.stats.cards_cached, 0)
        self.assertEqual(sellers["1000001"]["products"], ["ART_CACHED"])

    def test_touch_products_upserts_fresh_and_reuses_stale(self):
        self._seed()
        from datetime import datetime, timedelta, timezone

        # Записи старше 14 дней перестают считаться свежими
        stale = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE seller_products SET last_seen_at = ? WHERE article = 'ART_CACHED'",
                (stale,),
            )
        owners = self.store.product_owners(["ART_CACHED"])
        self.assertEqual(owners, {})
        # Продлеваем и снова видим
        self.store.touch_products([("ART_CACHED", "1000001")])
        owners = self.store.product_owners(["ART_CACHED"])
        self.assertEqual(owners, {"ART_CACHED": "1000001"})


# --- Полный прогон: первый и повторный --------------------------------------

SECTION_PATH = "/highlight/tovary-iz-kitaya-935133/"


class TestIncrementalRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SellerStore(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, section=SECTION_PATH):
        pages = {
            section: {
                "layout": [{
                    "stateId": "tileGridDesktop-1",
                    "component": "tileGrid-1",
                    # В реальном ответе Ozon params приходит СТРОКОЙ "{}" —
                    # от этого зависит отсев рекомендательных гридов
                    "params": "{}",
                }],
                "widgetStates": {
                    "tileGridDesktop-1": json.dumps({
                        "items": [{"id": "1001"}, {"id": "1002"}, {"id": "1003"}],
                    }),
                },
            },
            "/product/1001/": card_json("203104", "Магазин Китая"),
            "/product/1002/": card_json("203104", ""),
            "/product/1003/": card_json("999999", "Другой Магазин"),
        }
        modals = {
            "203104": modal_uscc(),
            "999999": modal_uscc(uscc="91360111MAC3F9Y20A"),
        }
        return FakeClient(pages=pages, modals=modals)

    def _run(self, client, refresh_days=7):
        harvester = ChinaSellerHarvester(client, store=self.store, refresh_days=refresh_days)
        return harvester.run(SECTION_PATH, pages=1), harvester

    def test_first_run_enriches_and_writes_metrics_at(self):
        client = self._client()
        stats, _ = self._run(client)
        self.assertEqual(stats.sellers_found, 2)
        self.assertEqual(stats.sellers_verified_uscc, 2)
        self.assertEqual(stats.requests, 1 + 3 + 2)        # раздел + 3 карточки + 2 модалки
        meta = self.store.sellers_meta()
        self.assertTrue(meta["203104"]["metrics_at"], "метрики должны быть записаны")
        self.assertEqual(meta["203104"]["verified_by"], VERIFY_USCC)
        # Склад (delivery-запрос) для USCC-продавца не нужен
        self.assertNotIn("delivery", [kind for kind, _ in client.calls])

    def test_second_run_skips_fresh_sellers(self):
        client = self._client()
        self._run(client)
        client2 = self._client()
        stats2, _ = self._run(client2)
        # Известных двое, метрики свежие — обе модалки пропущены
        self.assertEqual(stats2.sellers_skipped, 2)
        self.assertEqual(stats2.sellers_verified_uscc, 0)
        # Повторный прогон: только страница раздела, ни карточек (кэш), ни модалок
        self.assertEqual(stats2.requests, 1)
        self.assertNotIn(("page", "/product/1001/"), client2.calls)
        self.assertNotIn(("modal", "203104"), client2.calls)

    def test_refresh_days_zero_skips_everything_known(self):
        client = self._client()
        self._run(client)
        client2 = self._client()
        stats2, _ = self._run(client2, refresh_days=0)
        self.assertEqual(stats2.sellers_skipped, 2)
        self.assertEqual(stats2.requests, 1)              # только страница раздела

    def test_full_refresh_when_metrics_expired(self):
        client = self._client()
        self._run(client)
        # Искусственно состарим метрики
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
        with self.store._connect() as conn:
            conn.execute("UPDATE sellers SET metrics_at = ?", (old,))
        client2 = self._client()
        stats2, _ = self._run(client2)
        # Метрики протухли — модалки перечитываются, но карточки берутся из кэша
        self.assertEqual(stats2.sellers_skipped, 0)
        self.assertIn(("modal", "203104"), client2.calls)
        self.assertEqual(stats2.cards_cached, 3)
        self.assertEqual(stats2.products, 0)
        # Уровень достоверности не потерян
        meta = self.store.sellers_meta()
        self.assertEqual(meta["203104"]["verified_by"], VERIFY_USCC)

    def test_known_confirmed_not_downgraded_without_evidence(self):
        # Продавец подтверждён по складу; повторный прогон даёт пустую
        # модалку без USCC и без узла доставки — уровень должен сохраниться
        from src.outreach.scoring import SellerMetrics

        self.store.add_sellers([
            SellerMetrics(
                seller_id="1000001", shop_name="Магазин А", verified_by=VERIFY_WAREHOUSE,
                is_chinese=True,
                sample_product_url="https://www.ozon.ru/product/1001/",
            ),
        ])
        client = FakeClient(
            pages={"/product/1001/": card_json("1000001", "Магазин А")},
            modals={"1000001": {"widgetStates": {}}},       # пустая модалка: ни USCC, ни юрлица
        )
        harvester = ChinaSellerHarvester(client, store=self.store, refresh_days=7)
        harvester._known = self.store.sellers_meta()
        sellers = harvester.resolve_sellers(["1001"])
        result = harvester.enrich_seller("1000001", sellers["1000001"])
        self.assertEqual(result.verified_by, VERIFY_WAREHOUSE)
        self.assertIn("прежний уровень сохранён", result.chinese_reasons)
        # В пустой модалке нет USCC — запрос склада не делается и без узла доставки
        delivery_calls = [c for c in client.calls if c[0] == "delivery"]
        self.assertEqual(delivery_calls, [])


if __name__ == "__main__":
    unittest.main()