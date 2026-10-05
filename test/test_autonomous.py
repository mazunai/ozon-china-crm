#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты автономной обработки: атомарность очереди, риск-стоп, backoff,
переживание перезапуска без повторных отправок.

Запуск: python test/test_autonomous.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.risk import (                                  # noqa: E402
    DEFAULT_RISK_MARKERS,
    PlatformRestricted,
    load_extra_markers,
    match_risk_markers,
)
from src.crm.store import STATUS_NEW, STATUS_SENT, SellerStore  # noqa: E402
from src.notify.tgbot import parse_command                  # noqa: E402
from src.outreach.autonomous import (                       # noqa: E402
    AutonomousRunner,
    LockBusy,
)
from src.outreach.sender import backoff_seconds             # noqa: E402


def seed(store: SellerStore, seller_id: str, score: float = 1.0,
         priority: int = 0, metrics_at: str = "") -> None:
    """Продавец, которого очередь обязана взять: подтверждён и с товаром."""
    with store._connect() as conn:
        conn.execute(
            """INSERT INTO sellers (seller_id, shop_name, score, priority, status,
                   verified_by, sample_product_url, attempts, metrics_at,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, 'new', 'uscc', 'https://o.ru/p/1', 0, ?, 't', 't')""",
            (seller_id, seller_id, score, priority, metrics_at),
        )


def make_runner(tmp: Path, store: SellerStore, sent_log: list):
    """Раннер с Telegram-заглушкой: уведомления собираются в sent_log."""
    return AutonomousRunner(
        data_dir=tmp / "outreach_data",
        state_path=tmp / "data" / "autonomous_state.json",
        lock_path=tmp / "data" / "autonomous.lock",
        stop_path=tmp / "data" / "autonomous.stop",
        tg_send=lambda text: sent_log.append(text) or True,
    )


# --- Очередь: атомарность и приоритеты -------------------------------------


def test_claim_is_atomic_under_threads():
    """Восемь потоков на одной базе не уносят одного продавца дважды.

    Регрессия прежней гонки: SELECT и UPDATE были вне транзакции, два
    воркера получали одну строку, и продавцу уходило два сообщения.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sellers.sqlite3"
        store = SellerStore(db)
        for i in range(20):
            seed(store, f"s{i:02d}", score=float(i))

        claimed: list = []
        lock = threading.Lock()
        errors: list = []

        def worker():
            own = SellerStore(db)  # отдельное соединение, как у второго процесса
            try:
                while True:
                    row = own.claim_next()
                    if not row:
                        return
                    with lock:
                        claimed.append(row["seller_id"])
            except Exception as exc:                        # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(claimed) == len(set(claimed)), "одного продавца забрали дважды"
        assert len(claimed) == 20, f"взяли {len(claimed)} из 20"


def test_claim_returns_each_seller_once_without_marksent():
    """Захваченный («sending») продавец не выдаётся второму, пока не решён."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SellerStore(Path(tmp) / "s.sqlite3")
        seed(store, "s1")
        seed(store, "s2")

        first = store.claim_next()
        second = store.claim_next()
        assert first["seller_id"] != second["seller_id"]
        assert store.claim_next() is None  # оба в работе — очередь пуста

        # Аварийная выдача: зависший sending возвращается и берётся заново.
        # Спим больше секунды: last_attempt_at пишется с точностью до секунды,
        # и cutoff «сейчас» при строгом сравнении не поймает ту же секунду.
        time.sleep(1.2)
        assert store.release_stale(older_than_minutes=0) == 2
        again = store.claim_next()
        assert again["seller_id"] in ("s1", "s2")


def test_priority_orders_queue():
    """Ручной приоритет оператора двигает продавца вперёд, не трогая score."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SellerStore(Path(tmp) / "s.sqlite3")
        seed(store, "low", score=100)
        seed(store, "mid", score=50)
        seed(store, "high", score=10)

        row = store.claim_next()
        assert row["seller_id"] == "low", "без приоритета порядок по score"

        store.set_priority("high", 10)
        row = store.claim_next()
        assert row["seller_id"] == "high", "операторский priority должен быть первым"


def test_order_fresh_prefers_fresh_metrics():
    """order_fresh: сначала те, у кого свежее метрики, приоритет всё равно выше."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SellerStore(Path(tmp) / "s.sqlite3")
        seed(store, "stale", score=90, metrics_at="2026-01-01T00:00:00+00:00")
        seed(store, "fresh", score=10, metrics_at="2026-09-30T00:00:00+00:00")

        row = store.claim_next(order_fresh=True)
        assert row["seller_id"] == "fresh"


def test_sent_sellers_never_resent_after_restart():
    """Перезапуск процесса не отправляет повторно: sent не возвращается в очередь.

    Ключевой инвариант автономного режима: release_stale поднимает только
    зависшие «sending», а «sent» не трогает никогда.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "s.sqlite3"
        store = SellerStore(db)
        seed(store, "s1")
        seed(store, "s2")

        row = store.claim_next()
        store.mark_sent(row["seller_id"], "текст")  # успех записан

        # «Перезапуск»: новый экземпляр store на той же базе
        fresh = SellerStore(db)
        fresh.release_stale(older_than_minutes=0)  # даже агрессивная чистка
        nxt = fresh.claim_next()
        assert nxt["seller_id"] == "s2", "отправленному снова ушла очередь"
        assert fresh.claim_next() is None

        sent_status = fresh.get_seller("s1")["status"]
        assert sent_status == STATUS_SENT


# --- Risk Control ------------------------------------------------------------


def test_risk_markers_match_and_miss():
    assert match_risk_markers("Antibot Challenge Page") != ""
    assert match_risk_markers("Доступ ограничен: проблема с IP") != ""
    assert match_risk_markers("Подтвердите, что вы не робот") != ""
    # Обычная страница магазина — не риск
    assert match_risk_markers("OZON Каталог Мой Ozon Заказы Корзина") == ""
    assert match_risk_markers("") == ""


def test_risk_extra_markers_from_file():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        assert load_extra_markers(data_dir) == ()  # нет файла — пусто

        (data_dir / "risk_markers.json").write_text(
            '["новая заглушка ozon"]', encoding="utf-8")
        extra = load_extra_markers(data_dir)
        assert extra == ("новая заглушка ozon",)
        assert match_risk_markers("Новая Заглушка Ozon", extra) == "новая заглушка ozon"


def test_risk_incident_lifecycle():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SellerStore(Path(tmp) / "s.sqlite3")
        assert store.open_risk_incident() is None

        store.record_risk_incident("подозрительная активность", stage="открытие чата",
                                   seller_id="s1", detail="снимок страницы")
        incident = store.open_risk_incident()
        assert incident is not None and incident["marker"] == "подозрительная активность"
        assert incident["resolved_at"] == ""

        assert store.resolve_risk_incident("operator") is True
        assert store.open_risk_incident() is None
        assert store.resolve_risk_incident("operator") is False  # закрывать больше нечего


def test_platform_restricted_carries_details():
    from datetime import datetime, timedelta, timezone as tz

    long_text = "x" * 2000
    exc = PlatformRestricted("antibot challenge", snippet=long_text, stage="проверка входа")
    assert exc.marker == "antibot challenge"
    assert exc.stage == "проверка входа"
    assert len(exc.snippet) == 1500  # потолок: текст нужен целиком, чтобы вытащить срок
    short = PlatformRestricted("captcha", snippet="короткий")
    assert short.snippet == "короткий"  # короткий текст не режется


def test_antifraud_fake_network_page_marker():
    """Фейковая страница антифрода (маскируется под «нет соединения») — маркер.

    Ночь 02–03.10.2026: Ozon отдал заглушку «Похоже, нет соединения /
    Выключите VPN / Инцидент: fab.chlg_…» — это вердикт антибота, и прогон
    обязан остановиться Risk Control'ом, а не жечь очередь.
    """
    from src.crm.risk import match_risk_markers

    text = ("Похоже, нет соединения. Выключите VPN, перезагрузите роутер или "
            "подключитесь к другой сети. Инцидент: fab.chlg_20261002185123")
    assert match_risk_markers(text) != ""
    assert match_risk_markers("Похоже, нет соединения") != ""


def test_block_message_marker_and_resume_time():
    """Реальный текст лимита Ozon распознаётся, срок парсится в UTC.

    Регрессия 01.10: маркер был в именительном падеже и пропускал страницу
    «Мы заметили подозрительную активность, исходящую от вас...» — блок
    не останавливал прогон.

    Дата в тексте — ЗАВТРА: parse_resume_time считает прошедший срок
    относится к следующему году, и фиксированное «2 октября» падало бы
    2 октября после 13:43 UTC (проверено 02.10.2026).
    """
    from datetime import datetime, timedelta, timezone
    from src.crm.risk import parse_resume_time

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    month_names = ("января", "февраля", "марта", "апреля", "мая", "июня",
                   "июля", "августа", "сентября", "октября", "ноября", "декабря")
    day, month = tomorrow.day, month_names[tomorrow.month - 1]
    text = (f"Мы заметили подозрительную активность, исходящую от вас. "
            f"Новые сообщения можно будет отправить {day} {month} после 12:43 по UTC.")
    assert match_risk_markers(text) == "подозрительную активность"
    assert parse_resume_time(text) == f"{tomorrow.date().isoformat()}T12:43:00+00:00"
    assert parse_resume_time("Ошибка сети. Попробуйте ещё раз") == ""
    assert match_risk_markers("Новые сообщения можно будет отправить") != ""


# --- Backoff и предохранитель -------------------------------------------------


def test_backoff_grows_and_caps():
    assert backoff_seconds(1, 5, 60) == 0          # первый сбой — обычный интервал
    assert backoff_seconds(2, 5, 60) == 5 * 60
    assert backoff_seconds(3, 5, 60) == 10 * 60    # удвоение
    assert backoff_seconds(4, 5, 60) == 20 * 60
    assert backoff_seconds(9, 5, 60) == 60 * 60    # потолок
    assert backoff_seconds(20, 5, 60) == 60 * 60   # и не растёт дальше


# --- Автономный режим ----------------------------------------------------------


def test_tg_commands_parse():
    assert parse_command("/status") == ("/status", "")
    assert parse_command("/status extra") == ("/status", "extra")
    assert parse_command("/resume@my_bot") == ("/resume", "")
    assert parse_command("привет") == ("", "привет")


def test_autonomous_stops_gracefully_and_releases_lock():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sent_log: list = []
        runner = make_runner(Path(tmp), None, sent_log)
        runner.request_graceful_stop()  # кран стоит ещё до старта

        rounds: list = []
        runner.run_forever(
            start_round=lambda: rounds.append(1),
            round_running=lambda: False,
            sleep_fn=lambda s: None,
        )
        assert rounds == [], "при штатном кране раунды не запускаются"
        assert runner.status().get("running") is False
        assert not runner.lock_path.exists(), "лок не освобождён"


def test_autonomous_counts_rounds_and_stops_on_manual_stop():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sent_log: list = []
        runner = make_runner(Path(tmp), None, sent_log)
        waits: list = []

        # Раунд «успешно пуст»: первый вызов round_running — идёт, второй — кончился
        states = iter([True, False])
        snapshot = {"sent": 2, "failed": 0, "skipped": 1,
                    "stopped_reason": "очередь пуста"}
        runner._wait = lambda minutes, sleep_fn, extra_check: waits.append(minutes)

        def start_round():
            rounds = runner.status().get("rounds", 0)
            if rounds >= 2:
                # после двух раундов оператор жмёт стоп
                runner.request_graceful_stop()

        runner.run_forever(
            start_round=start_round,
            round_running=lambda: next(states, False),
            round_snapshot=lambda: snapshot,
            sleep_fn=lambda s: None,
        )
        assert runner.status()["rounds"] == 2
        assert runner.status()["sent"] == 4
        assert runner.status()["skipped"] == 2
        assert waits == [60, 60], "пустая очередь даёт часовой интервал"
        assert any("остановлена" in text for text in sent_log)


def test_autonomous_risk_pause_requires_operator():
    """При риск-стопе раунды не запускаются, пока оператор не сделает resume."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = SellerStore(tmp_path / "s.sqlite3")
        sent_log: list = []
        runner = make_runner(tmp_path, store, sent_log)

        # Отправщик уже поставил стоп-кран и записал инцидент
        (tmp_path / "outreach_data").mkdir()
        (tmp_path / "outreach_data" / "STOP").write_text("risk", encoding="utf-8")
        store.record_risk_incident("antibot challenge", stage="проверка входа")

        rounds: list = []
        # Цикл в риск-паузе не выйдет сам — останавливаем его из таймера,
        # как это сделал бы оператор после разбора.
        stop_timer = threading.Timer(0.2, runner.request_graceful_stop)
        stop_timer.start()
        runner.run_forever(
            start_round=lambda: rounds.append(1),
            round_running=lambda: False,
            sleep_fn=lambda s: time.sleep(0.01),
        )
        stop_timer.join()
        assert rounds == [], "риск-пауза не запускает раунды"
        assert "ограничение платформы" in runner.status().get("paused_reason", "")
        assert any("⚠️" in text for text in sent_log)

        # Оператор разобрался: инцидент закрыт, кран снят — раунды пойдут
        result = runner.resume(store)
        assert result["had_incident"] is True
        assert not (tmp_path / "outreach_data" / "STOP").exists()
        assert store.open_risk_incident() is None

        # Оператор запускает заново (как /auto_start): снимается и штатный
        # кран, оставленный остановкой цикла из таймера выше.
        runner.cancel_graceful_stop()
        started: list = []

        def first_round():
            started.append(1)
            runner.request_graceful_stop()  # один раунд — и штатный выход

        runner.run_forever(
            start_round=first_round,
            round_running=lambda: False,
            sleep_fn=lambda s: None,
        )
        assert len(started) == 1, "после resume отправка продолжается"


def test_autonomous_lock_blocks_second_process():
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(Path(tmp), None, [])
        runner.take_lock()
        # Чужой живой pid в локе — второй «процесс» не должен захватить режим.
        runner.lock_path.write_text(str(os.getppid()), encoding="utf-8")
        try:
            runner.take_lock()
            raise AssertionError("ожидали LockBusy")
        except LockBusy:
            pass
        # release_lock чужой файл не трогает — это защита чужого процесса
        assert runner.lock_path.exists()
        # Возвращаем свой pid — теперь перезапуск в том же процессе возможен
        runner.lock_path.write_text(str(os.getpid()), encoding="utf-8")
        runner.take_lock()
        runner.release_lock()
        assert not runner.lock_path.exists()


def test_autonomous_state_survives_reload():
    """Стейт атомарно пишется и читается заново — перезапуск не теряет счётчики."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(Path(tmp), None, [])
        with runner._lock:
            runner.state.update({"rounds": 7, "sent": 13, "running": True})
            runner._save_state()

        second = make_runner(Path(tmp), None, [])
        reloaded = second.status()
        assert reloaded["rounds"] == 7
        assert reloaded["sent"] == 13
        assert reloaded["running"] is True  # дашборд по этому флагу продолжит режим


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
