#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты разбора метрик и оценки продавцов.

Строки взяты из реальных ответов Ozon, чтобы тест ловил регрессию, если Ozon
поменяет формат. Запуск: python test/test_outreach.py
"""

import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.outreach.scoring import (
    ScoreWeights,
    SellerMetrics,
    filter_chinese,
    format_report,
    parse_count,
    parse_rating,
    parse_tenure_months,
    score_all,
    sort_sellers,
)
from src.outreach.sender import (
    SenderConfig,
    in_window,
    parse_window,
    plan_next_delay,
    should_send_now,
)


def test_parse_count():
    """Форматы из живых ответов модалки продавца."""
    cases = [
        ("702", 702),
        ("260", 260),
        ("106 K", 106_000),
        ("33,9 K", 33_900),
        ("11,8 K", 11_800),
        ("53,5 K", 53_500),
        ("9 369", 9_369),          # обычный пробел как разделитель разрядов
        ("3\u202f338", 3_338),     # узкий неразрывный пробел
        ("1,2 M", 1_200_000),
        ("", 0),
        (None, 0),
        ("нет данных", 0),
    ]
    for raw, expected in cases:
        got = parse_count(raw)
        assert got == expected, f"parse_count({raw!r}) = {got}, ожидалось {expected}"


def test_parse_tenure_months():
    """«3 месяца» -> 3, «1 год» -> 12 — всё в месяцах, чтобы сравнивать."""
    cases = [
        ("3 месяца", 3),
        ("11 месяцев", 11),
        ("1 месяц", 1),
        ("1 год", 12),
        ("2 года", 24),
        ("4 года", 48),
        ("5 лет", 60),
        ("", 0),
    ]
    for raw, expected in cases:
        got = parse_tenure_months(raw)
        assert got == expected, f"parse_tenure_months({raw!r}) = {got}, ожидалось {expected}"


def test_parse_rating():
    cases = [("3,3 из 5", 3.3), ("4,7 из 5", 4.7), ("4,9 из 5", 4.9), ("4.1", 4.1), ("", 0.0)]
    for raw, expected in cases:
        got = parse_rating(raw)
        assert abs(got - expected) < 1e-9, f"parse_rating({raw!r}) = {got}, ожидалось {expected}"


def test_score_ranks_bigger_seller_higher():
    """Крупный и долгоживущий продавец должен обгонять мелкого.

    Значения взяты из реальных продавцов: senyi2dian (702 заказа, 3 месяца)
    против happyflywithmestore (106 K заказов, 1 год).
    """
    small = SellerMetrics.from_raw(
        "5050453", orders_count="702", reviews_count="260",
        working_time="3 месяца", average_rating="3,3 из 5", shop_name="senyi2dian",
    )
    big = SellerMetrics.from_raw(
        "2948681", orders_count="106 K", reviews_count="53,5 K",
        working_time="1 год", average_rating="4,7 из 5", shop_name="happyflywithmestore",
    )
    score_all([small, big])
    assert big.score > small.score, f"big={big.score} должен быть выше small={small.score}"


def test_score_parts_present():
    """Разбивка по слагаемым попадает в metrics — по ней видно, чем набран балл."""
    seller = SellerMetrics.from_raw("1", orders_count="1 K", reviews_count="100",
                                   working_time="1 год", average_rating="5 из 5")
    score_all([seller])
    assert set(seller.score_parts) == {"orders", "reviews", "tenure", "rating"}
    total = sum(seller.score_parts.values())
    assert abs(total - seller.score) < 0.01, "сумма слагаемых не совпала со score"


def test_weights_change_order():
    """Веса реально влияют на порядок, а не являются декорацией."""
    a = SellerMetrics.from_raw("a", orders_count="100 K", reviews_count="1",
                               working_time="1 месяц", average_rating="1 из 5")
    b = SellerMetrics.from_raw("b", orders_count="1", reviews_count="100",
                               working_time="120 месяцев", average_rating="5 из 5")

    score_all([a, b], ScoreWeights(orders=10, reviews=0.1, tenure=0, rating=0))
    assert a.score > b.score, "при упоре на заказы должен выигрывать a"

    score_all([a, b], ScoreWeights(orders=0.1, reviews=0, tenure=1, rating=10))
    assert b.score > a.score, "при упоре на срок и рейтинг должен выигрывать b"


def test_sort_best_last():
    """«Самый классный в самый низ» — режим по умолчанию."""
    sellers = [
        SellerMetrics.from_raw("weak", orders_count="10", reviews_count="1",
                               working_time="1 месяц", average_rating="2 из 5"),
        SellerMetrics.from_raw("strong", orders_count="100 K", reviews_count="50 K",
                               working_time="36 месяцев", average_rating="4,9 из 5"),
    ]
    score_all(sellers)

    best_last = sort_sellers(sellers, best_last=True)
    assert best_last[-1].seller_id == "strong", "лучший должен быть в конце"

    best_first = sort_sellers(sellers, best_last=False)
    assert best_first[0].seller_id == "strong", "с --best-first лучший сверху"


def test_filter_chinese():
    cn = SellerMetrics.from_raw("cn", orders_count="1 K", is_chinese=True)
    rf = SellerMetrics.from_raw("rf", orders_count="1 K", is_chinese=False)
    assert [s.seller_id for s in filter_chinese([cn, rf])] == ["cn"]


def test_format_report_handles_empty():
    assert "пуст" in format_report([]).lower()


def test_window_and_limits():
    """Ограничители: окно, включая переход через полночь."""
    assert in_window(time(12, 0), time(10, 0), time(19, 0)) is True
    assert in_window(time(9, 0), time(10, 0), time(19, 0)) is False
    assert in_window(time(20, 0), time(10, 0), time(19, 0)) is False

    # Окно через полночь: 22:00–06:00
    assert in_window(time(23, 30), time(22, 0), time(6, 0)) is True
    assert in_window(time(5, 0), time(22, 0), time(6, 0)) is True
    assert in_window(time(12, 0), time(22, 0), time(6, 0)) is False

    assert parse_window("10:00", "19:00") == (time(10, 0), time(19, 0))


def test_should_send_now_respects_cap_and_window():
    config = SenderConfig(interval_minutes=60, daily_cap=5,
                          window_start="10:00", window_end="19:00", dry_run=False)

    # Внутри окна и лимит не выбран — можно
    decision = should_send_now(datetime(2026, 9, 25, 12, 0), config, sent_last_24h=0)
    assert decision.allowed, decision.reason

    # Лимит выбран — нельзя
    decision = should_send_now(datetime(2026, 9, 25, 12, 0), config, sent_last_24h=5)
    assert not decision.allowed and "лимит" in decision.reason

    # Ночью — нельзя
    decision = should_send_now(datetime(2026, 9, 25, 3, 0), config, sent_last_24h=0)
    assert not decision.allowed and "окна" in decision.reason

    # Пауза между сообщениями
    decision = should_send_now(
        datetime(2026, 9, 25, 12, 0), config, sent_last_24h=0,
        next_allowed_at=datetime(2026, 9, 25, 12, 30),
    )
    assert not decision.allowed and decision.retry_in_seconds == 1800


def test_dry_run_ignores_cap():
    """В предпросмотре лимит не должен мешать дойти до первого же продавца."""
    config = SenderConfig(daily_cap=0, dry_run=True)
    decision = should_send_now(datetime(2026, 9, 25, 12, 0), config, sent_last_24h=999)
    assert decision.allowed, decision.reason


def test_plan_next_delay_in_range():
    """Задержка попадает в интервал ± разброс и никогда не уходит в минус."""
    import random

    config = SenderConfig(interval_minutes=60, interval_jitter_minutes=10)
    rng = random.Random(42)
    for _ in range(50):
        delay = plan_next_delay(config, rng)
        assert 50 * 60 <= delay <= 70 * 60, f"задержка вне диапазона: {delay}"

    # Нулевой интервал не должен давать нулевую паузу
    assert plan_next_delay(SenderConfig(interval_minutes=0, interval_jitter_minutes=0)) >= 60


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
