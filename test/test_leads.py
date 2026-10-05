#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты Lead Intelligence (раздел 11): скоринг, градации, воронка,
мультиаккаунт-защита от дублей, эксперименты.

Запуск: python test/test_leads.py
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.leads import (                                 # noqa: E402
    GRADES,
    LeadConfig,
    commercial_score,
    engagement_score,
    final_priority,
    grade_for,
    score_lead,
)
from src.crm.store import SellerStore                       # noqa: E402
from src.outreach.sender import (                           # noqa: E402
    SenderConfig,
    repeat_contact_allowed,
)
from src.outreach.templates import TemplatePool             # noqa: E402


def make_store(tmp: Path) -> SellerStore:
    return SellerStore(tmp / "sellers.sqlite3")


def add_seller(store: SellerStore, seller_id: str, **fields) -> None:
    defaults = dict(shop_name=seller_id, score=5.0, orders=1000, reviews=200,
                    months_on_ozon=12, rating=4.2, verified_by="uscc",
                    status="new", product_count=50, province="广东",
                    uscc="USCC123", metrics_at="2026-09-30T00:00:00+00:00",
                    sample_product_url="https://o.ru/p/1")
    defaults.update(fields)
    columns = ", ".join(["seller_id", "created_at", "updated_at", *defaults])
    placeholders = ", ".join(["?"] * (3 + len(defaults)))
    with store._connect() as conn:
        conn.execute(
            f"INSERT INTO sellers ({columns}) VALUES ({placeholders})",
            (seller_id, "t", "t", *defaults.values()),
        )


# --- §11.2 Commercial / Engagement -------------------------------------------


def test_commercial_score_explainable():
    row = {"orders": 150000, "reviews": 40000, "months_on_ozon": 30,
           "rating": 4.8, "product_count": 320, "metrics_at":
           "2026-09-30T00:00:00+00:00", "uscc": "X", "province": "广东"}
    result = commercial_score(row, LeadConfig())
    assert 0 <= result["commercial"] <= 100
    assert 0.8 < result["components"]["orders"] < 1.0, "лог-шкала: 150k близко к референсу"
    assert commercial_score({**row, "orders": 1_000_000},
                            LeadConfig())["components"]["orders"] == 1.0
    assert any("крупный объём" in r for r in result["reasons"])
    assert any("высокий рейтинг" in r for r in result["reasons"])
    assert result["completeness"] == 1.0


def test_commercial_incomplete_data_lowers_confidence():
    full = commercial_score({"orders": 5000, "reviews": 500, "months_on_ozon": 10,
                             "rating": 4.0, "product_count": 100,
                             "metrics_at": "2026-09-30T00:00:00+00:00",
                             "uscc": "X", "province": "广东"}, LeadConfig())
    empty = commercial_score({"orders": 5000}, LeadConfig())
    assert empty["completeness"] < full["completeness"]
    assert empty["commercial"] < full["commercial"], "пустота данных занижает балл"
    assert any("нет данных" in r for r in empty["reasons"])


def test_engagement_by_categories_and_confidence():
    none = engagement_score([])
    assert none["engagement"] == 50.0 and none["confidence"] == "низкая"
    assert any("не было" in r for r in none["reasons"])

    interested = engagement_score(["interested"])
    assert interested["engagement"] == 80.0
    assert interested["confidence"] == "низкая", "один факт — уверенность низкая"

    refused = engagement_score(["interested", "negative"])
    assert refused["engagement"] < 50.0, "отказ перевешивает"

    confident = engagement_score(["interested"] * 5)
    assert confident["confidence"] == "высокая"
    assert confident["engagement"] == 100.0, "потолок шкалы"


def test_final_priority_mix():
    config = LeadConfig({"priority_mix": 0.7})
    value = final_priority(80, 50, config)
    assert value == round(0.7 * 80 + 0.3 * 50, 1)
    assert 0 <= final_priority(0, 0, config) <= 100


def test_grade_thresholds_and_unqualified():
    config = LeadConfig({"grade_A": 75, "grade_B": 60, "grade_C": 40})
    verified = {"verified_by": "uscc"}
    assert grade_for(80, verified, config)[0] == "A"
    assert grade_for(65, verified, config)[0] == "B"
    assert grade_for(45, verified, config)[0] == "C"
    assert grade_for(10, verified, config)[0] == "D"
    heuristic = {"verified_by": "heuristic"}
    assert grade_for(99, heuristic, config) == (
        "U", "нет подтверждения китайского происхождения")
    assert set(GRADES) == {"A", "B", "C", "D", "U"}


def test_score_lead_integration_via_store():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")
        store.log_message("s1", "запрос", "received")
        # категория ставится только на существующую строку продавца:
        store.set_reply_category("s1", "interested")

        card = store.recompute_lead_score("s1")
        assert card is not None
        assert card["grade"] in ("A", "B", "C", "D")
        assert card["engagement"] > 50.0, "интерес поднимает вовлечённость"

        row = store.get_seller("s1")
        assert row["lead_grade"] == card["grade"], "кэш записан"
        assert row["commercial_score"] == card["commercial"]


# --- §11.3 градации вручную -----------------------------------------------------


def test_manual_grade_with_reason():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")
        assert store.set_lead_grade("s1", "B", reason="менеджер знает магазин") is True
        row = store.get_seller("s1")
        assert row["lead_grade"] == "B"
        assert row["grade_source"] == "manual"
        assert "менеджер" in row["grade_reason"]
        try:
            store.set_lead_grade("s1", "X", reason="")
            raise AssertionError("ожидали ValueError")
        except ValueError:
            pass


# --- §11.7 воронка ----------------------------------------------------------------


def test_funnel_transitions_from_existing_actions():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")

        store.mark_sent("s1", "текст")            # contacted
        store.set_reply_category("s1", "interested")  # interested
        events = store.funnel_transitions(10)
        stages = [e["to_stage"] for e in events]
        assert "contacted" in stages and "interested" in stages
        counts = store.funnel_counts()
        assert counts["contacted"] >= 1 and counts["interested"] >= 1

        # отказ от рассылки — глобальный, событие в воронке
        store.mark_opted_out("s1")
        assert store.funnel_counts()["opted_out"] >= 1

        # сделку ставит ТОЛЬКО оператор (set_stage), actor='operator'
        store.set_stage("s1", "deal")
        deal_events = [e for e in store.funnel_transitions(10)
                       if e["to_stage"] == "deal"]
        assert deal_events and deal_events[0]["actor"] == "operator"


def test_no_automatic_deal_path():
    """Регрессия §11.7: авто-код не переводит в Deal ни при каком ответе.

    set_reply_category ('interested') не должен создавать событие 'deal'.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")
        store.mark_sent("s1", "текст")
        store.set_reply_category("s1", "interested")
        store.set_reply_category("s1", "positive")
        assert all(e["to_stage"] != "deal"
                   for e in store.funnel_transitions(50)), \
            "найден автоматический путь в Deal"


# --- §11.5 мультиаккаунт --------------------------------------------------------


def test_prior_contact_detects_other_account():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        add_seller(store, "s1")
        store.mark_sent("s1", "текст", sender_account_id="manager_a")

        prior = store.prior_contact("s1", sender_account_id="manager_b")
        assert prior is not None, "чужая отправка должна быть видна другому аккаунту"
        assert prior["sender_account_id"] == "manager_a"

        # Своих отправок в фильтре «другие» нет
        assert store.prior_contact("s1", sender_account_id="manager_a") is None


def test_repeat_policy_never_and_after_days():
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(days=5)).isoformat(timespec="seconds")
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")

    # never: любой прошлый контакт запрещает
    allowed, reason = repeat_contact_allowed("never", 30, recent, now)
    assert not allowed and "запрещены" in reason

    # after_days: свежий контакт — нельзя, старый — можно
    allowed, reason = repeat_contact_allowed("after_days", 30, recent, now)
    assert not allowed and "через" in reason
    allowed, _ = repeat_contact_allowed("after_days", 30, old, now)
    assert allowed

    # кривая дата при after_days — запрет (без данных разрешения нет)
    allowed, reason = repeat_contact_allowed("after_days", 30, "мусор", now)
    assert not allowed


def test_sender_config_defaults_multikaccount_safe():
    config = SenderConfig()
    assert config.repeat_contact_policy == "never", \
        "по умолчанию повторные контакты запрещены"
    assert config.sender_account_id, "аккаунт отправителя должен быть задан"


def test_old_database_migrates_new_columns():
    """База до v1.17 открывается и получает новые колонки и таблицы.

    Имитируем РЕАЛЬНУЮ старую базу (v1.14): весь базовый набор колонок есть,
    новых (priority, лид-поля, аккаунты отправителя) нет.
    """
    import tempfile

    old_sellers_columns = (
        "seller_id TEXT PRIMARY KEY, shop_name TEXT, company_name TEXT, uscc TEXT, "
        "province TEXT, orders INTEGER, reviews INTEGER, months_on_ozon INTEGER, "
        "rating REAL, score REAL, is_chinese INTEGER, sample_product_url TEXT, "
        "status TEXT, attempts INTEGER, last_attempt_at TEXT, last_error TEXT, "
        "message_sent TEXT, created_at TEXT, updated_at TEXT, notes TEXT, "
        "seller_profile_url TEXT, verified_by TEXT, registration_kind TEXT, "
        "address TEXT, first_seen_at TEXT, last_seen_at TEXT, product_count INTEGER, "
        "chinese_reasons TEXT, sent_at TEXT, metrics_at TEXT, stage TEXT, "
        "reply_category TEXT, reply_at TEXT, handed_to_manager_at TEXT, "
        "reviews_seen INTEGER, replies_seen INTEGER, reviews_checked_at TEXT"
    )
    old_messages_columns = (
        "id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id TEXT, text TEXT, "
        "sent_at TEXT, result TEXT, error TEXT, category TEXT, template_name TEXT"
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute(f"CREATE TABLE sellers ({old_sellers_columns})")
        conn.execute(
            "INSERT INTO sellers (seller_id, created_at, updated_at) "
            "VALUES ('s1', 't', 't')")
        conn.execute(f"CREATE TABLE messages ({old_messages_columns})")
        conn.commit()
        conn.close()

        store = SellerStore(db)  # открываем ИМЕННО старую базу — миграция на месте
        check = sqlite3.connect(db)
        check.row_factory = sqlite3.Row
        columns = {r["name"] for r in check.execute("PRAGMA table_info(messages)")}
        sellers_columns = {r["name"] for r in check.execute("PRAGMA table_info(sellers)")}
        check.close()
        for column in ("sender_account_id", "campaign_id", "experiment_id",
                       "platform_message_id"):
            assert column in columns, f"нет колонки messages.{column}"
        for column in ("priority", "lead_grade", "commercial_score", "next_task"):
            assert column in sellers_columns, f"нет колонки sellers.{column}"
        add_seller(store, "s2")  # база жива
        assert store.funnel_counts()["imported"] >= 2


# --- §11.4 эксперименты ---------------------------------------------------------


def test_experiment_assignment_deterministic_and_report():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        for i in range(6):
            add_seller(store, f"s{i}", score=float(i))
        eid = store.create_experiment("Тест A/B", {"min_score": 0},
                                      ["msg01", "msg02"])
        assigned = store.assign_experiment_sellers(
            eid, ["s0", "s1", "s2", "s3", "s4", "s5"], ["msg01", "msg02"])
        assert assigned == 6

        first = {sid: store.experiment_for_seller(sid)["variant"]
                 for sid in ("s0", "s1", "s2")}
        second = {sid: store.experiment_for_seller(sid)["variant"]
                  for sid in ("s0", "s1", "s2")}
        assert first == second, "назначение не воспроизводимо"

        report = store.experiment_report(eid)
        assert {v["variant"] for v in report["variants"]} == {"msg01", "msg02"}
        assert all("baseline" in v for v in report["variants"])
        assert "мало данных" in report["variants"][0]["confidence"]

        store.finish_experiment(eid)
        assert store.experiment_for_seller("s0") is None, \
            "завершённый эксперимент не должен диктовать текст"


def test_template_pool_get_by_name():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for name in ("msg01", "msg02"):
            (directory / f"{name}.txt").write_text(
                f"Текст {name}. Если не актуально — напишите, больше не буду писать.",
                encoding="utf-8")
        pool = TemplatePool.load(directory)
        assert pool.get("msg02").name == "msg02"
        assert pool.get("нет-такого") is None


# --- §11.6 категории ответов -------------------------------------------------------


def test_public_category_mapping():
    from src.crm.leads import PUBLIC_REPLY_CATEGORIES, PUBLIC_CATEGORIES
    assert PUBLIC_REPLY_CATEGORIES["interested"] == "Positive"
    assert PUBLIC_REPLY_CATEGORIES["needs_info"] == "Question"
    assert PUBLIC_REPLY_CATEGORIES["negative"] == "Refusal"
    assert PUBLIC_REPLY_CATEGORIES["optout"] == "Opt-out"
    assert PUBLIC_REPLY_CATEGORIES["other"] == "Unknown"
    assert set(PUBLIC_REPLY_CATEGORIES.values()) <= set(PUBLIC_CATEGORIES) | {"Positive"}


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
