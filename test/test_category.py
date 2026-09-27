"""
Офлайн-самопроверка нишевых категорий: извлечение из хлебных крошек карточки,
журнал замеров в базе, актуальная категория продавца, фасеты и статистика.

Структуры breadCrumbs сняты с живого API 27.09.2026 (см. AGENTS.md §3.7).

Запуск: PYTHONPATH=. python test/test_category.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.category import extract_category  # noqa: E402
from src.crm.store import SellerStore  # noqa: E402

PASSED = 0
TOTAL = 0


def check(condition, description):
    global PASSED, TOTAL
    TOTAL += 1
    mark = "OK " if condition else "FAIL"
    PASSED += 1 if condition else 0
    print(f"  [{mark}] {description}")
    return condition


# Живые образцы хлебных крошек из карточек (26-27.09.2026)
CARD_SHADOW = {"widgetStates": {
    "breadCrumbs-3385917-default-1": json.dumps({"breadcrumbs": [
        {"text": "Красота и здоровье", "link": "/category/krasota-i-zdorove-6500/"},
        {"text": "Макияж", "link": "/category/makiyazh-6501/"},
        {"text": "Глаза", "link": "/category/kosmetika-dlya-glaz-6516/"},
        {"text": "Тени", "link": "/category/teni-6518/"},
        {"text": "Eighteen years", "link": "/category/teni-6518/eighteen-years-101817735/"},
    ], "connectSymbol": ">", "isFull": True}),
    "webPrice-1": '{"price": [101]}',
}}

CARD_SOCKS = {"widgetStates": {
    "breadCrumbs-3385917-default-1": json.dumps({"breadcrumbs": [
        {"text": "Одежда", "link": "/category/odezhda-obuv-i-aksessuary-7500/"},
        {"text": "Женская одежда", "link": "/category/zhenskaya-odezhda-7501/"},
        {"text": "Носки, колготки и чулки", "link": "/category/chulki-noski-kolgotki-zhenskie-31688/"},
        {"text": "Носки", "link": "/category/noski-zhenskie-36471/"},
    ], "connectSymbol": ">", "isFull": True}),
}}


def test_extract():
    print("Извлечение категории из хлебных крошек")
    cat = extract_category(CARD_SHADOW)
    check(cat and cat["path"] == "Красота и здоровье > Макияж > Глаза > Тени",
          "путь собирается из крошек")
    check(cat["leaf"] == "Тени" and cat["category_id"] == "6518",
          "лист и id категории корректны")
    check(cat["depth"] == 4, "страница бренда отфильтрована (двухсегментная ссылка)")

    cat2 = extract_category(CARD_SOCKS)
    check(cat2["leaf"] == "Носки" and cat2["category_id"] == "36471",
          "вторая карточка: лист «Носки»")

    check(extract_category({"widgetStates": {}}) is None,
          "без виджета breadCrumbs — None")
    check(extract_category({"widgetStates": {
        "breadCrumbs-1": json.dumps({"breadcrumbs": []})}}) is None,
        "пустые крошки — None")
    check(extract_category({"widgetStates": {"breadCrumbs-1": "{битый json"}}) is None,
        "битый JSON — None")


def test_store(tmp: Path):
    print("Журнал категорий: запись, актуальная категория, фасеты, статистика")
    store = SellerStore(tmp / "cats.sqlite3")
    metrics = [
        dict(seller_id="s1", verified_by="uscc"),
        dict(seller_id="s2", verified_by="uscc"),
        dict(seller_id="s3", verified_by="none"),   # неподтверждённый
    ]
    import sqlite3
    conn = sqlite3.connect(tmp / "cats.sqlite3")
    for m in metrics:
        conn.execute(
            "INSERT INTO sellers (seller_id, created_at, updated_at, verified_by) "
            "VALUES (?, datetime('now'), datetime('now'), ?)", (m["seller_id"], m["verified_by"]))
        conn.commit()

    store.record_category("s1", "1687474411",
                          extract_category(CARD_SHADOW))
    # второй замер того же продавца: актуален последний
    store.record_category("s1", "1687474411", {"path": "Другое", "leaf": "Другое",
                                               "category_id": "9999"})
    store.record_category("s2", "3580930405", extract_category(CARD_SOCKS))

    cmap = store.seller_categories_map()
    check(cmap["s1"]["category_leaf"] == "Другое",
          "актуальная категория — по последнему замеру (история не переписана)")
    check(len(store.categorized_seller_ids()) == 2, "двое из трёх имеют замеры")

    facets = dict(store.niche_facets())
    check(facets.get("Тени") is None and facets.get("Другое") == 1
          and facets.get("Носки") == 1,
          "фасеты только по подтверждённым и по актуальному замеру")

    # статистика активности: s2 ответил, s1 нет
    conn.execute("INSERT INTO messages (seller_id, text, sent_at, result) "
                 "VALUES ('s2', 'да, интересно', datetime('now'), 'received')")
    conn.commit()
    stats = {r["leaf"]: r for r in store.category_stats()}
    check(stats["Носки"]["sellers"] == 1 and stats["Носки"]["replied"] == 1,
          "статистика «ниша × ответы» по журналу сообщений")
    check(stats["Другое"]["replied"] == 0, "не ответившие считаются нулём")

    # фильтр списка по нише
    rows = store.list_sellers(niches=["Носки"], verified_only=True)
    check([r["seller_id"] for r in rows] == ["s2"]
          and rows[0]["niche"] == "Носки",
          "фильтр по нише и колонка niche в выборке")
    check(len(store.list_sellers(niches=["Носки"], verified_only=True, include_unverified=True)) == 1,
          "без фильтра достоверности ниша тоже работает")


def run():
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        test_extract()
        test_store(tmp)
    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(run())
