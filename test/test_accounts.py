#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мультиаккаунт отправщика: реестр аккаунтов, лимиты по аккаунту,
аккаунтные стоп-краны.

Запуск: python test/test_accounts.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.store import SellerStore                       # noqa: E402
from src.outreach.accounts import (                         # noqa: E402
    MAIN_ID,
    SenderAccount,
    add_account,
    get_account,
    load_accounts,
    remove_account,
    save_accounts,
)
from src.outreach.sender import SenderConfig, run_queue     # noqa: E402
from src.outreach.templates import MessageTemplate          # noqa: E402


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


def add_message(store: SellerStore, seller_id: str, sent_at: str,
                sender_account_id: str, result: str = "sent",
                campaign: str = "") -> None:
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO messages (seller_id, text, sent_at, result, campaign_id, "
            "sender_account_id) VALUES (?, ?, ?, ?, ?, ?)",
            (seller_id, "текст", sent_at, result, campaign, sender_account_id),
        )


# --- Реестр аккаунтов -------------------------------------------------------


def test_default_registry_without_file():
    """Нет accounts.json — один main с историческими профилем и портом."""
    with tempfile.TemporaryDirectory() as tmp:
        accounts = load_accounts(Path(tmp))
        assert len(accounts) == 1 and accounts[0].id == MAIN_ID
        assert str(accounts[0].profile_dir()) == str(Path.home() / ".ozon_parser_chrome")
        assert accounts[0].debug_port() == 9222


def test_add_account_assigns_ports_and_persists():
    """Добавление аккаунтов: порты 9223+, профиль из id; дубли и main защищены."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr2 = add_account(Path(tmp), "mgr2", label="Менеджер 2")
        assert mgr2.debug_port() == 9223
        assert str(mgr2.profile_dir()) == str(Path.home() / ".ozon_parser_chrome_mgr2")

        mgr3 = add_account(Path(tmp), "mgr3")
        assert mgr3.debug_port() == 9224

        # Перезагрузка из файла — те же аккаунты и порты
        loaded = {acc.id: acc for acc in load_accounts(Path(tmp))}
        assert set(loaded) == {MAIN_ID, "mgr2", "mgr3"}
        assert loaded["mgr2"].debug_port() == 9223
        assert loaded["mgr2"].label == "Менеджер 2"

        try:
            add_account(Path(tmp), "mgr2")
            raise AssertionError("дубль аккаунта должен падать")
        except ValueError:
            pass
        try:
            remove_account(Path(tmp), MAIN_ID)
            raise AssertionError("main удалять нельзя")
        except ValueError:
            pass
        remove_account(Path(tmp), "mgr3")
        assert set(a.id for a in load_accounts(Path(tmp))) == {MAIN_ID, "mgr2"}


def test_explicit_port_and_profile_win():
    """Явные профиль и порт в реестре главнее выводимых из id."""
    with tempfile.TemporaryDirectory() as tmp:
        save_accounts(Path(tmp), [
            SenderAccount(id=MAIN_ID, port=0),
            SenderAccount(id="mgr2", profile="/tmp/custom_chrome", port=9500),
        ])
        loaded = {acc.id: acc for acc in load_accounts(Path(tmp))}
        assert loaded[MAIN_ID].debug_port() == 9222
        assert str(loaded["mgr2"].profile_dir()) == "/tmp/custom_chrome"
        assert loaded["mgr2"].debug_port() == 9500


def test_implicit_ports_by_slot():
    """Аккаунтам без порта он выводится по своей позиции: 9223, 9224, …"""
    with tempfile.TemporaryDirectory() as tmp:
        save_accounts(Path(tmp), [
            SenderAccount(id=MAIN_ID),
            SenderAccount(id="mgr2"),
            SenderAccount(id="mgr3"),
        ])
        loaded = {acc.id: acc for acc in load_accounts(Path(tmp))}
        assert loaded["mgr2"].debug_port() == 9223
        assert loaded["mgr3"].debug_port() == 9224


def test_stop_file_name():
    """Стоп-кран main — общий STOP, у остальных — персональный STOP_<id>."""
    assert SenderAccount(id=MAIN_ID).stop_file_name("STOP") == "STOP"
    assert SenderAccount(id="mgr2").stop_file_name("STOP") == "STOP_mgr2"


def test_apply_to_config_overrides():
    """Переопределения лимитов аккаунта применяются, None — не трогает."""
    config = SenderConfig(daily_cap=12, interval_minutes=60,
                          interval_jitter_minutes=10,
                          window_start="10:00", window_end="19:00")
    SenderAccount(id="mgr2", daily_cap=8, window_start="09:00").apply_to_config(config)
    assert config.daily_cap == 8
    assert config.window_start == "09:00"
    assert config.interval_minutes == 60
    assert config.interval_jitter_minutes == 10
    assert config.window_end == "19:00"


def test_get_account_unknown_id():
    """Неизвестный id — понятная ошибка со списком существующих."""
    with tempfile.TemporaryDirectory() as tmp:
        add_account(Path(tmp), "mgr2")
        try:
            get_account(Path(tmp), "mgr9")
            raise AssertionError("неизвестный аккаунт должен падать")
        except ValueError as exc:
            assert "mgr2" in str(exc)


# --- Лимиты и очередь по аккаунтам ------------------------------------------


def test_sent_today_per_account():
    """Дневной лимит считается по сообщениям аккаунта; пустая метка — эпоха main."""
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        add_message(store, "s1", now, "main")
        add_message(store, "s2", now, "mgr2")
        add_message(store, "s3", now, "mgr2")
        add_message(store, "s4", now, "")  # старая запись без метки аккаунта

        assert store.sent_today() == 4
        assert store.sent_today("mgr2") == 2
        assert store.sent_today("main") == 1


def test_followup_candidates_by_account():
    """Фоллоу-ап пишется только СВОИМ контактам; записи без метки — контакты main."""
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
        for seller_id, account in (("by_main", "main"),
                                   ("by_mgr2", "mgr2"),
                                   ("legacy", "")):
            add_seller(store, seller_id)
            add_message(store, seller_id, old, account)

        main_ids = [r["seller_id"] for r in store.followup_candidates(14, 10, sender_account_id="main")]
        assert set(main_ids) == {"by_main", "legacy"}
        mgr2_ids = [r["seller_id"] for r in store.followup_candidates(14, 10, sender_account_id="mgr2")]
        assert mgr2_ids == ["by_mgr2"]
        all_ids = [r["seller_id"] for r in store.followup_candidates(14, 10)]
        assert len(all_ids) == 3, "без фильтра — все кандидаты"


def test_run_queue_stop_crane():
    """Глобальный стоп-кран останавливает прогон (одна сессия)."""
    template = MessageTemplate(name="t", body="Привет, {shop_name}!")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "STOP").write_text("operator\n", encoding="utf-8")
        report = run_queue(
            queue=make_store(data_dir), template=template,
            config=SenderConfig(dry_run=True), data_dir=data_dir, once=True,
        )
        assert report["stopped_reason"] == "сработал стоп-кран"


def test_run_queue_without_crane_goes_on():
    """Без стоп-крана прогон работает: доходит до своей причины стопа."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        report = run_queue(
            queue=make_store(data_dir), template=MessageTemplate(name="t", body="Привет!"),
            config=SenderConfig(dry_run=True), data_dir=data_dir, once=True,
        )
        assert report["stopped_reason"] == "очередь пуста"


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
