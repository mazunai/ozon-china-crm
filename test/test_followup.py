#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты фоллоу-апа и ежедневных механик (вежливые вторые касания).

Запуск: python test/test_followup.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.store import SellerStore                       # noqa: E402
from src.outreach.followup import (                         # noqa: E402
    FollowupConfig,
    load_followup_template,
    run_followup_round,
)


def make_store(tmp: Path) -> SellerStore:
    return SellerStore(tmp / "sellers.sqlite3")


def add_seller(store: SellerStore, seller_id: str, **fields) -> None:
    defaults = dict(shop_name=seller_id, score=5.0, orders=1000, reviews=200,
                    months_on_ozon=12, rating=4.2, verified_by="uscc",
                    status="sent", product_count=50, province="广东",
                    uscc="USCC123", sample_product_url="https://o.ru/p/1",
                    sent_at="2026-09-01T00:00:00+00:00")
    defaults.update(fields)
    columns = ", ".join(["seller_id", "created_at", "updated_at", *defaults])
    placeholders = ", ".join(["?"] * (3 + len(defaults)))
    with store._connect() as conn:
        conn.execute(
            f"INSERT INTO sellers ({columns}) VALUES ({placeholders})",
            (seller_id, "t", "t", *defaults.values()),
        )


def add_message(store: SellerStore, seller_id: str, result: str,
                sent_at: str, category: str = "", campaign: str = "") -> None:
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO messages (seller_id, text, sent_at, result, category, campaign_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seller_id, "текст", sent_at, result, category, campaign),
        )


NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=20)).isoformat(timespec="seconds")
RECENT = (NOW - timedelta(days=3)).isoformat(timespec="seconds")


def test_followup_candidates_selection():
    """Отбор честный: молчит >14 дней без ответов — кандидат; остальные нет."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "silent")           # написано 20 дней назад, ответа нет
        add_seller(store, "answered")         # ответил — не трогаем
        add_message(store, "answered", "received", OLD)
        add_seller(store, "fresh", sent_at=RECENT)  # написано 3 дня назад — рано
        add_seller(store, "refused")          # статус refused — не трогаем
        with store._connect() as conn:
            conn.execute("UPDATE sellers SET status='refused' WHERE seller_id='refused'")
        add_seller(store, "already")          # фоллоу-ап уже был
        add_message(store, "already", "sent", OLD, campaign="followup")

        ids = [r["seller_id"] for r in store.followup_candidates(14, 10)]
        assert ids == ["silent"], ids


def test_followup_candidates_respects_limit_and_order():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        for i in range(5):
            # чем раньше написали — тем раньше в очереди фоллоу-апа
            add_seller(store, f"s{i}",
                       sent_at=(NOW - timedelta(days=30 - i)).isoformat(timespec="seconds"))
        rows = store.followup_candidates(14, 2)
        assert [r["seller_id"] for r in rows] == ["s0", "s1"], "самые старые — первыми"


def test_followup_config_roundtrip():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "followup.json"
        config = FollowupConfig(enabled=True, after_days=10, limit_per_round=3)
        config.save(path)
        loaded = FollowupConfig.load(path)
        assert loaded.enabled is True
        assert loaded.after_days == 10
        assert loaded.limit_per_round == 3
        # битый файл — молча умолчания, без падений
        path.write_text("{битый json", encoding="utf-8")
        assert FollowupConfig.load(path).enabled is False


def test_followup_template_guard():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        assert load_followup_template(data_dir) is None  # файла нет

        (data_dir / "followup.txt").write_text(
            "ЗАМЕНИТЕ ЭТОТ ТЕКСТ", encoding="utf-8")
        assert load_followup_template(data_dir) is None  # заготовка

        (data_dir / "followup.txt").write_text(
            "Здравствуйте! Напоминаю о себе. Если не актуально — не побеспокою.",
            encoding="utf-8")
        template = load_followup_template(data_dir)
        assert template is not None and template.name == "followup"


def test_run_followup_round_disabled_and_no_template():
    """Выключенный фоллоу-ап и отсутствие текста не открывают браузер вовсе."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")
        config = FollowupConfig(enabled=False)
        report = run_followup_round(store, config, Path(tmp), dry_run=True)
        assert report["sent"] == 0 and "выключены" in report["reason"]

        config = FollowupConfig(enabled=True)
        report = run_followup_round(store, config, Path(tmp), dry_run=True)
        assert "followup.txt" in report["reason"]


def test_run_followup_round_dry_run_preview():
    """Предпросмотр показывает текст на реальном кандидате и ничего не шлёт."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        store = make_store(data_dir)
        add_seller(store, "s1", shop_name="Магазин №1")
        (data_dir / "followup.txt").write_text(
            "Здравствуйте, {shop_name}! Напоминаю о себе. "
            "Если не актуально — не побеспокою.", encoding="utf-8")
        config = FollowupConfig(enabled=True, after_days=14)
        report = run_followup_round(store, config, data_dir, dry_run=True)
        assert report["preview"]["seller_id"] == "s1"
        assert "Магазин №1" in report["preview"]["text"]
        assert store.followup_candidates(14, 10), "предпросмотр не расходует кандидатов"


def test_inbox_stats_hot_split():
    """Статистика входящих: hot считаются только по горячим категориям."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        add_seller(store, "s1", status="replied")
        add_seller(store, "s2", status="replied")
        add_seller(store, "s3", status="replied")
        add_message(store, "s1", "received", recent, category="interested")
        add_message(store, "s2", "received", recent, category="negative")
        add_message(store, "s3", "received", "2026-08-01T00:00:00+00:00", category="interested")

        stats = store.inbox_stats(24)
        assert stats["replies"] == 2, "старый ответ не входит в 24ч"
        assert stats["hot"] == 1, "отказ не считается горячим"


def test_requeue_transient_chat_failures():
    """«Чат не открылся» возвращается в очередь; прочие ошибки и свежие — нет."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "old_chat_fail", status="failed")
        add_seller(store, "fresh_chat_fail", status="failed")
        add_seller(store, "other_fail", status="failed")
        now = datetime.now(timezone.utc)
        old_at = (now - timedelta(hours=2)).isoformat(timespec="seconds")
        fresh_at = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        with store._connect() as conn:
            for seller_id, error, at in (
                ("old_chat_fail", "чат не открылся: поле ввода не появилось", old_at),
                ("fresh_chat_fail", "чат не открылся: поле ввода не появилось", fresh_at),
                ("other_fail", "не подтвердилась отправка", old_at),
            ):
                conn.execute(
                    "UPDATE sellers SET status='failed', last_error=?, last_attempt_at=? "
                    "WHERE seller_id=?", (error, at, seller_id))

        assert store.requeue_transient_chat_failures(older_than_minutes=60) == 1
        assert store.get_seller("old_chat_fail")["status"] == "new"
        assert store.get_seller("fresh_chat_fail")["status"] == "failed", "час не прошёл"
        assert store.get_seller("other_fail")["status"] == "failed", "другая ошибка не возвращается"


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {name}: {exc}")
        except Exception as exc:                            # noqa: BLE001
            failed += 1
            print(f"  ERR  {name}: {type(exc).__name__}: {exc}")

    print(f"\nВсего: {len(tests)}, провалено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
