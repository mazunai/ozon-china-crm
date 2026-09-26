"""
Офлайн-самопроверка «живости» продавца: уверенность по Лапласу, категории
для фильтра, опознание ответа продавца в комментариях и честный учёт сэмпла.

Не требует сети — только чистые функции и JSON, снятые с живого API
26.09.2026 (структуры см. в AGENTS.md §3.6).

Запуск: PYTHONPATH=. python test/test_vitality.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.review_sample import (          # noqa: E402
    count_shop_replies,
    extract_reviews_url,
    is_shop_reply,
    plan_sample,
    reviews_count,
)
from src.crm.vitality import (               # noqa: E402
    CAT_MIXED,
    CAT_REPLIES,
    CAT_SILENT,
    CAT_UNKNOWN,
    sql_matches,
    vitality_category,
    vitality_payload,
    vitality_score,
)

PASSED = 0
TOTAL = 0


def check(condition, description):
    global PASSED, TOTAL
    TOTAL += 1
    mark = "OK " if condition else "FAIL"
    PASSED += 1 if condition else 0
    print(f"  [{mark}] {description}")
    return condition


def test_score_and_categories():
    print("Уверенность (Лаплас) и категории")
    # Сама формула: (replies + 1) / (seen + 2)
    check(abs(vitality_score(5, 4) - 5 / 7) < 1e-9, "score(видел=5, ответил=4) == 5/7")
    check(abs(vitality_score(0, 0) - 0.5) < 1e-9, "score(0,0) == 0.5 (без данных нейтрально)")
    # «Мало данных»: меньше трёх рассмотренных отзывов
    check(vitality_category(2, 0) == CAT_UNKNOWN, "2 отзыва — мало данных")
    check(vitality_category(0, 0) == CAT_UNKNOWN, "0 отзывов — мало данных")
    check(vitality_category(3, 0) == CAT_SILENT, "0 из 3 — молчит")
    check(vitality_category(5, 0) == CAT_SILENT, "0 из 5 — молчит")
    check(vitality_category(3, 2) == CAT_REPLIES, "2 из 3 — отвечает")
    check(vitality_category(5, 4) == CAT_REPLIES, "4 из 5 — отвечает")
    check(vitality_category(3, 1) == CAT_MIXED, "1 из 3 — иногда отвечает")
    check(vitality_category(10, 2) == CAT_MIXED, "2 из 10 — иногда отвечает")
    check(vitality_category(10, 1) == CAT_SILENT, "1 из 10 — молчит")
    # payload несёт честные числа для интерфейса
    p = vitality_payload(5, 4)
    check(p["label"] == "отвечает" and p["replies"] == 4 and p["seen"] == 5,
          "payload: «отвечает 4/5» с числами из базы")


def test_sql_equivalents():
    print("SQL-эквиваленты фильтра совпадают с категориями")
    mismatch = None
    for seen in range(0, 40):
        for replies in range(0, seen + 1):
            cat = vitality_category(seen, replies)
            sql_replies = sql_matches("replies", seen, replies)
            sql_silent = sql_matches("silent", seen, replies)
            if cat == CAT_REPLIES and not sql_replies:
                mismatch = (cat, seen, replies)
            if cat == CAT_SILENT and not sql_silent:
                mismatch = (cat, seen, replies)
            if cat in (CAT_MIXED, CAT_UNKNOWN) and (sql_replies or sql_silent):
                mismatch = (cat, seen, replies)
    check(mismatch is None,
          "пороги SQL и Python совпадают на сетке 40×40" +
          ("" if mismatch is None else f" (расхождение: {mismatch})"))


def test_shop_reply_detection():
    print("Опознание ответа продавца в комментариях")
    # Живой пример ответа магазина OUYA01 (товар 3697313189, 26.09.2026)
    shop_reply = {
        "comment": "Иван, спасибо за такой тёплый отзыв! Приходите к нам ещё!",
        "author": {
            "clientOfficial": {"id": "30743165", "partnerType": 1, "name": "OUYA01",
                               "brandUri": "https://www.ozon.ru/seller/ouya01/"},
            "firstName": "OUYA01",
        },
        "isUserComment": False,
    }
    check(is_shop_reply(shop_reply), "комментарий магазина (clientOfficial.id != 0) — ответ продавца")
    # Живой пример комментария покупателя Аля (товар 2781343413)
    # ВАЖНО: isUserComment у неё ТОЖЕ False — по нему отличать нельзя.
    buyer_comment = {
        "comment": "Длинный комментарий покупательницы…",
        "author": {
            "clientOfficial": {"id": "0", "partnerType": 0, "name": ""},
            "firstName": "Аля",
        },
        "isUserComment": False,
    }
    check(not is_shop_reply(buyer_comment), "комментарий покупателя (id=0) — не ответ продавца")
    check(not is_shop_reply({"author": {}}), "пустой автор — не ответ продавца")
    check(count_shop_replies([shop_reply, buyer_comment, {}]) == 1,
          "в списке из трёх комментариев ровно один ответ продавца")


def test_sample_accounting():
    print("Честный учёт сэмпла на карточке")
    card = {"widgetStates": {
        "webReviewProductScore-3132021-default-1": '{"url": "product/slug-1687474411/reviews", "reviewsCount": 2150}',
        "webCurrentSeller-1": '{"sellerId": "2278246"}',
    }}
    check(extract_reviews_url(card) == "/product/slug-1687474411/reviews",
          "адрес страницы отзывов берётся из счётчика карточки")
    check(reviews_count(card) == 2150, "число отзывов из счётчика карточки")
    check(extract_reviews_url({"widgetStates": {}}) == "" and reviews_count(card) > 0,
          "без виджета-счётчика адрес пустой — сэмпл не снимается")

    def review(uuid, total):
        return {"uuid": uuid, "comments": {"list": [], "totalCount": total}}

    reviews = [review(f"u{i}", total) for i, total in enumerate([0, 0, 1, 0, 2, 3, 0])]
    anchors, unresolved = plan_sample(reviews, max_anchors=2)
    # Комментарии есть у трёх отзывов, якорим только двух: один остаётся
    # неразобранным и из сэмпла выпадает (иначе он ложно считался бы молчуном)
    check(len(anchors) == 2 and unresolved == 1, "якорим максимум 2, неразобранный выпадает")
    seen = len(reviews) - unresolved
    check(seen == 6, f"в сэмпл попадает {seen} из 7 отзывов")
    anchors0, unresolved0 = plan_sample(reviews[:2], max_anchors=2)
    check(not anchors0 and unresolved0 == 0 and len(reviews[:2]) - unresolved0 == 2,
          "отзывы без комментариев разрешаются без якорных запросов")


def run():
    test_score_and_categories()
    test_sql_equivalents()
    test_shop_reply_detection()
    test_sample_accounting()
    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(run())
