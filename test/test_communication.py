#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты коммуникации: категории ответов, сохранение полного текста,
списки (не ответили / заинтересованные), передача менеджеру, миграция.

Главное требование проекта — ни один ответ продавца не должен теряться,
поэтому большая часть проверок про текст: сохраняется целиком, дата не
теряется, старая база мигрирует без потерь.

Запуск: PYTHONPATH=. python3 test/test_communication.py
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.classify import classify_reply
from src.crm.inbox import filter_incoming_bubbles, parse_composer_date
from src.crm.store import (
    INTERESTED_CATEGORIES,
    REPLY_CATEGORY_LABELS,
    SellerStore,
)


def _seller(seller_id: str, shop: str = "", score: float = 5.0):
    from src.outreach.scoring import SellerMetrics

    return SellerMetrics.from_raw(
        seller_id=seller_id,
        orders_count="100",
        reviews_count="50",
        working_time="6 месяцев",
        average_rating="4.8",
        shop_name=shop,
        company_name=f"Company {seller_id}",
    )


class TestClassify(unittest.TestCase):
    def test_negative_before_positive(self):
        """«НЕ интересно» должно победить «интересно» — порядок правил важен."""
        self.assertEqual(classify_reply("Спасибо, но нам это не интересно."), "negative")
        self.assertEqual(classify_reply("Не нужно, не беспокойте"), "negative")
        self.assertEqual(classify_reply("Не актуально"), "negative")
        self.assertEqual(classify_reply("Откажемся, спасибо"), "negative")

    def test_interested(self):
        self.assertEqual(classify_reply("Да, нам интересно."), "interested")
        self.assertEqual(classify_reply("Можно обсудить условия."), "interested")
        self.assertEqual(classify_reply("Готовы сотрудничать"), "interested")
        # смешанный ответ: заинтересованность сильнее просьбы о деталях
        self.assertEqual(
            classify_reply("Да, нам интересно. Расскажите подробнее."), "interested")

    def test_needs_info(self):
        self.assertEqual(classify_reply("Расскажите подробнее..."), "needs_info")
        self.assertEqual(classify_reply("Какие условия?"), "needs_info")
        self.assertEqual(classify_reply("Сколько стоит доставка?"), "needs_info")
        # «пришлите прайс» — просьба о материалах, не готовность к сделке
        self.assertEqual(classify_reply("Пришлите прайс"), "needs_info")

    def test_positive(self):
        self.assertEqual(classify_reply("Спасибо большое!"), "positive")
        self.assertEqual(classify_reply("Благодарю за обращение"), "positive")

    def test_neutral(self):
        self.assertEqual(classify_reply("Здравствуйте!"), "neutral")
        self.assertEqual(classify_reply("Добрый день"), "neutral")
        self.assertEqual(classify_reply("ок"), "neutral")

    def test_other(self):
        self.assertEqual(classify_reply("У нас склад в Иу, заходите"), "other")
        self.assertEqual(classify_reply(""), "other")
        self.assertEqual(classify_reply(None), "other")


class TestSaveReply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SellerStore(Path(self.tmp.name) / "test.sqlite3")
        self.store.add_sellers([_seller("1", "YM Store"), _seller("2", "KJ LED")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_text_saved_verbatim(self):
        """Полный текст сохраняется как есть — без обрезки и префиксов."""
        long_reply = "Здравствуйте! " * 40 + "Нам интересно сотрудничество."
        self.store.save_reply("1", long_reply, "2026-09-25T14:30:00+00:00")
        history = self.store.message_history("1")
        self.assertEqual(history[0]["text"], long_reply)
        self.assertEqual(history[0]["result"], "received")
        self.assertEqual(history[0]["sent_at"], "2026-09-25T14:30:00+00:00")

    def test_seller_fields_updated(self):
        category = self.store.save_reply("1", "Да, интересно. Расскажите подробнее.",
                                         "2026-09-25T10:00:00+00:00")
        self.assertEqual(category, "interested")
        row = self.store.get_seller("1")
        self.assertEqual(row["status"], "replied")
        self.assertEqual(row["reply_category"], "interested")
        self.assertEqual(row["reply_at"], "2026-09-25T10:00:00+00:00")

    def test_second_reply_keeps_history(self):
        """Повторный ответ не затирает предыдущий: журнал растёт."""
        self.store.save_reply("1", "первый ответ", "2026-09-25T10:00:00+00:00")
        self.store.save_reply("1", "второй ответ", "2026-09-26T10:00:00+00:00")
        history = self.store.message_history("1")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["text"], "второй ответ")
        row = self.store.get_seller("1")
        self.assertEqual(row["reply_at"], "2026-09-26T10:00:00+00:00")

    def test_opted_out_not_flipped(self):
        self.store.mark_opted_out("2")
        self.store.save_reply("2", "а всё-таки интересно")
        self.assertEqual(self.store.get_seller("2")["status"], "opted_out")

    def test_empty_reply_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_reply("1", "   ")

    def test_manual_category_overrides(self):
        """Ручная правка меняет категорию и на продавце, и на последнем ответе."""
        self.store.save_reply("1", "Расскажите подробнее", "2026-09-25T10:00:00+00:00")
        self.store.set_reply_category("1", "interested")
        self.assertEqual(self.store.get_seller("1")["reply_category"], "interested")
        self.assertEqual(self.store.message_history("1")[0]["category"], "interested")

    def test_manual_category_validated(self):
        with self.assertRaises(ValueError):
            self.store.set_reply_category("1", "непонятно-что")

    def test_update_reply_text_only_grows(self):
        """Задним числом текст обновляется, только если он длиннее превью."""
        self.store.save_reply("1", "Да", "2026-09-25T10:00:00+00:00")
        # равной длины и короче — перезаписи быть не должно
        self.assertFalse(self.store.update_reply_text("1", "Да", "2026-09-25T11:00:00+00:00"))
        self.assertFalse(self.store.update_reply_text("1", "Ок", "2026-09-25T11:00:00+00:00"))
        self.assertEqual(self.store.message_history("1")[0]["text"], "Да")
        self.assertEqual(self.store.get_seller("1")["reply_at"], "2026-09-25T10:00:00+00:00")
        # длинный полный текст — перезаписывается вместе с датой и категорией
        full = "Да, нам интересно сотрудничество. Расскажите про условия и цены."
        self.assertTrue(self.store.update_reply_text("1", full, "2026-09-25T10:05:00+00:00"))
        latest = self.store.message_history("1")[0]
        self.assertEqual(latest["text"], full)
        self.assertEqual(latest["sent_at"], "2026-09-25T10:05:00+00:00")
        self.assertEqual(latest["category"], "interested")
        seller = self.store.get_seller("1")
        self.assertEqual(seller["reply_at"], "2026-09-25T10:05:00+00:00")

    def test_update_reply_text_empty_ignored(self):
        self.store.save_reply("1", "что-то", "2026-09-25T10:00:00+00:00")
        self.assertFalse(self.store.update_reply_text("1", "   "))


class TestLists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SellerStore(Path(self.tmp.name) / "test.sqlite3")
        self.store.add_sellers([
            _seller("m1", "писали-ответили", 9),
            _seller("m2", "писали-молчит", 8),
            _seller("m3", "не писали", 7),
            _seller("m4", "негативный", 6),
        ])
        # В живой базе список дашборда показывает только подтверждённых:
        # помечаем всех как USCC, чтобы фильтры были как в бою.
        for seller_id in ("m1", "m2", "m3", "m4"):
            self.store.set_verification_reason(seller_id, "uscc", "тест")
        self.store.mark_sent("m1", "наш текст")
        self.store.mark_sent("m2", "наш текст")
        self.store.mark_sent("m4", "наш текст")
        self.store.save_reply("m1", "Да, интересно, обсудим условия",
                              "2026-09-25T10:00:00+00:00")
        self.store.save_reply("m4", "Спасибо, но не интересно",
                              "2026-09-25T11:00:00+00:00")

    def tearDown(self):
        self.tmp.cleanup()

    def test_communication_counts(self):
        counts = self.store.communication_counts()
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["messaged"], 3)
        self.assertEqual(counts["replied"], 2)
        self.assertEqual(counts["no_reply"], 1)
        self.assertEqual(counts["handed"], 0)

    def test_no_reply_is_messaged_only(self):
        """«Не ответили» — только те, кому писали; m3 без отправки не в списке."""
        rows = self.store.comm_report_rows(replied=False, messaged=True)
        self.assertEqual([r["seller_id"] for r in rows], ["m2"])

    def test_replied_list(self):
        rows = self.store.comm_report_rows(replied=True)
        self.assertEqual(set(r["seller_id"] for r in rows), {"m1", "m4"})

    def test_category_filter(self):
        rows = self.store.comm_report_rows(categories=["negative"])
        self.assertEqual([r["seller_id"] for r in rows], ["m4"])

    def test_interested_excludes_negative(self):
        rows = self.store.interested_rows()
        self.assertEqual([r["seller_id"] for r in rows], ["m1"])
        self.assertIn("интересно", rows[0]["last_reply_text"])

    def test_category_counts(self):
        counts = self.store.category_counts()
        self.assertEqual(counts.get("interested"), 1)
        self.assertEqual(counts.get("negative"), 1)
        self.assertEqual(sum(counts.values()), 2)

    def test_handover(self):
        self.store.hand_to_manager("m1")
        row = self.store.get_seller("m1")
        self.assertNotEqual(row["handed_to_manager_at"], "")
        self.assertEqual(row["stage"], "negotiation")
        counts = self.store.communication_counts()
        self.assertEqual(counts["handed"], 1)
        # передача не выбрасывает продавца из заинтересованных
        self.assertEqual([r["seller_id"] for r in self.store.interested_rows()], ["m1"])

    def test_recategorize_refills_empty(self):
        """После recategorize пустых категорий во входящих не остаётся."""
        self.store.save_reply("m2", "да, интересно", "2026-09-26T09:00:00+00:00")
        # эмулируем запись, сделанную до появления классификатора: категория пуста
        with self.store._connect() as conn:
            conn.execute("UPDATE messages SET category = '' WHERE seller_id = 'm2'")
        changed = self.store.recategorize()
        self.assertGreaterEqual(changed, 1)
        received = [m for m in self.store.message_history("m2") if m["result"] == "received"]
        self.assertEqual(received[0]["category"], "interested")
        # категория продавца подтянута из журнала
        self.assertEqual(self.store.get_seller("m2")["reply_category"], "interested")

    def test_list_sellers_filters_by_category(self):
        rows = self.store.list_sellers(reply_categories=["interested"])
        self.assertEqual([r["seller_id"] for r in rows], ["m1"])


class TestInboxHelpers(unittest.TestCase):
    def test_parse_composer_date(self):
        self.assertEqual(
            parse_composer_date("2026-09-26T02:00:58.711922Z"), "2026-09-26T02:00:58+00:00")
        self.assertEqual(parse_composer_date(""), "")
        self.assertEqual(parse_composer_date("мусор"), "")

    def test_filter_drops_system_and_own(self):
        """Служебные сообщения Ozon и наши тексты не должны попасть в ответ."""
        bubbles = [
            "Вы написали продавцу из Китая. Возможно, ответ придётся подождать.",
            "Дорогой друг, чем я могу тебе помочь?",
            "Здравствуйте! Магазин YM Store!\n\nЕсли не актуально — напишите.",
            "Да, пришлите прайс-лист",
        ]
        kept = filter_incoming_bubbles(
            bubbles, own_texts=["Здравствуйте! Магазин YM Store!\n\nЕсли не актуально — напишите."])
        self.assertEqual(kept, ["Дорогой друг, чем я могу тебе помочь?",
                                "Да, пришлите прайс-лист"])

    def test_filter_drops_assistant_marker(self):
        """Пузырь ассистента выкидывается целиком, даже многострочный."""
        bubbles = [
            "Ассистент Ozon\nЧтобы перевод был правильным, проверьте текст.\n00:03",
            "Расскажите подробнее",
        ]
        self.assertEqual(filter_incoming_bubbles(bubbles), ["Расскажите подробнее"])

    def test_filter_survives_garbage(self):
        self.assertEqual(filter_incoming_bubbles([]), [])
        self.assertEqual(filter_incoming_bubbles([None, "", "   "]), [])


class TestMigration(unittest.TestCase):
    def test_legacy_db_gets_columns_without_loss(self):
        """Старая база (без категории и reply-полей) мигрирует, данные живы."""
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            # Схема уровня «до CRM-категорий»: sellers без reply_*, messages без category
            conn.executescript("""
                CREATE TABLE sellers (
                    seller_id TEXT PRIMARY KEY,
                    shop_name TEXT NOT NULL DEFAULT '',
                    company_name TEXT NOT NULL DEFAULT '',
                    uscc TEXT NOT NULL DEFAULT '',
                    province TEXT NOT NULL DEFAULT '',
                    orders INTEGER NOT NULL DEFAULT 0,
                    reviews INTEGER NOT NULL DEFAULT 0,
                    months_on_ozon INTEGER NOT NULL DEFAULT 0,
                    rating REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    is_chinese INTEGER NOT NULL DEFAULT 0,
                    sample_product_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    message_sent TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    seller_profile_url TEXT NOT NULL DEFAULT '',
                    verified_by TEXT NOT NULL DEFAULT 'none',
                    registration_kind TEXT NOT NULL DEFAULT 'unknown',
                    address TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    product_count INTEGER NOT NULL DEFAULT 0,
                    chinese_reasons TEXT NOT NULL DEFAULT '',
                    metrics_at TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id TEXT NOT NULL, text TEXT NOT NULL,
                    sent_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'sent',
                    error TEXT NOT NULL DEFAULT ''
                );
            """)
            conn.execute(
                "INSERT INTO sellers (seller_id, shop_name, status, score, message_sent, "
                "created_at, updated_at) "
                "VALUES ('old1', 'Старый магазин', 'sent', 5.0, 'привет', 'a', 'b')")
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result) "
                "VALUES ('old1', 'старый ответ продавца', '2026-09-20T10:00:00+00:00', 'received')")
            conn.commit()
            conn.close()

            store = SellerStore(db_path)
            check = sqlite3.connect(db_path)
            check.row_factory = sqlite3.Row
            seller_cols = {r["name"] for r in check.execute("PRAGMA table_info(sellers)")}
            message_cols = {r["name"] for r in check.execute("PRAGMA table_info(messages)")}
            self.assertIn("reply_category", seller_cols)
            self.assertIn("handed_to_manager_at", seller_cols)
            self.assertIn("category", message_cols)
            # старый ответ жив и после открытия получил категорию по правилам? —
            # нет: recategorize отдельная команда, миграция ничего не переписывает
            row = check.execute(
                "SELECT text, result, category FROM messages WHERE seller_id='old1'").fetchone()
            self.assertEqual(row["text"], "старый ответ продавца")
            self.assertEqual(row["result"], "received")
            self.assertEqual(row["category"], "")
            # новый ответ в migrated-базу сохраняется штатно
            store.save_reply("old1", "Да, интересно", "2026-09-26T12:00:00+00:00")
            self.assertEqual(store.get_seller("old1")["reply_category"], "interested")
            check.close()
        finally:
            tmp.cleanup()

    def test_handover_cli_entry(self):
        """CLI-вход handover: по id, по имени, по подстроке (аудит: вход
        для менеджера был не тестирован)."""
        import argparse
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_crm", Path(__file__).parent.parent / "run_crm.py")
        run_crm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_crm)

        with tempfile.TemporaryDirectory() as tmp:
            store = SellerStore(Path(tmp) / "t.sqlite3")
            store.add_sellers([_seller("555", "Ozon Мебель")])
            # CLI читает DB_PATH из глобала модуля — подменяем на временную базу
            run_crm.DB_PATH = Path(tmp) / "t.sqlite3"

            # по id
            args = argparse.Namespace(seller="555", note="")
            code = run_crm.cmd_handover(args)
            self.assertEqual(code, 0)
            row = store.get_seller("555")
            self.assertNotEqual(row["handed_to_manager_at"], "")
            self.assertEqual(row["stage"], "negotiation")

            # по подстроке имени
            args = argparse.Namespace(seller="мебель", note="")
            code = run_crm.cmd_handover(args)
            self.assertEqual(code, 0)

            # несуществующий — код 1 и без изменений
            args = argparse.Namespace(seller="никого-такого", note="")
            code = run_crm.cmd_handover(args)
            self.assertEqual(code, 1)

    def test_interested_categories_constant(self):
        self.assertIn("interested", INTERESTED_CATEGORIES)
        self.assertIn("positive", INTERESTED_CATEGORIES)
        self.assertIn("needs_info", INTERESTED_CATEGORIES)
        self.assertNotIn("negative", INTERESTED_CATEGORIES)
        # у каждой категории есть русская метка для отчётов
        for key in INTERESTED_CATEGORIES:
            self.assertIn(key, REPLY_CATEGORY_LABELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
