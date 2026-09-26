"""Живость продавца: отвечает ли он на отзывы покупателей.

Источник данных — сэмпл отзывов, снятый с карточек товаров во время сбора
(src/crm/review_sample.py): у продавца копятся reviews_seen (сколько отзывов
рассмотрено) и replies_seen (сколько с ответом магазина).

Уверенность — сглаживание Лапласа: score = (replies + 1) / (seen + 2).
Сглаживание нужно, чтобы продавец с одним отзывом без ответа не получал
«0% отвечает» с той же уверенностью, что продавец с сотней. Цифры в
интерфейсе показываются как есть («отвечает 4/5»), без процентов-обещаний.

Модуль намеренно чистый (без SQLite и сети): и хранилище, и интерфейс, и
тесты должны считать живость одинаково.
"""

from typing import Dict

# Ниже этого числа рассмотренных отзывов вердикта нет — «мало данных».
MIN_SAMPLE = 3

# Пороги уверенности для фильтра «только молчуны / только отвечающие».
# Промежуточные значения — «иногда отвечает», их фильтр не показывает.
REPLIES_THRESHOLD = 0.5   # score >= 0.5 — отвечает
SILENT_THRESHOLD = 0.25   # score < 0.25 — молчит

CAT_REPLIES = "replies"
CAT_SILENT = "silent"
CAT_MIXED = "mixed"
CAT_UNKNOWN = "unknown"

VITALITY_LABELS: Dict[str, str] = {
    CAT_REPLIES: "отвечает",
    CAT_SILENT: "молчит",
    CAT_MIXED: "иногда отвечает",
    CAT_UNKNOWN: "мало данных",
}


def vitality_score(reviews_seen: int, replies_seen: int) -> float:
    """Сглаженная доля ответов: (replies + 1) / (seen + 2)."""
    return (replies_seen + 1) / (reviews_seen + 2)


def vitality_category(reviews_seen: int, replies_seen: int) -> str:
    """Категория живости: мало данных / молчит / иногда отвечает / отвечает."""
    if reviews_seen < MIN_SAMPLE:
        return CAT_UNKNOWN
    score = vitality_score(reviews_seen, replies_seen)
    if score >= REPLIES_THRESHOLD:
        return CAT_REPLIES
    if score < SILENT_THRESHOLD:
        return CAT_SILENT
    return CAT_MIXED


def vitality_payload(reviews_seen: int, replies_seen: int) -> Dict:
    """Срез для API/интерфейса: числа, категория, метка и уверенность."""
    category = vitality_category(reviews_seen, replies_seen)
    return {
        "seen": reviews_seen,
        "replies": replies_seen,
        "category": category,
        "label": VITALITY_LABELS[category],
        "score": round(vitality_score(reviews_seen, replies_seen), 4),
    }


# --- SQL-эквиваленты категорий ---------------------------------------------
# SQLite не умеет вызывать Python, поэтому пороги выражены алгебраически.
# Вывод: score >= 0.5  <=>  2*(r+1) >= s+2  <=>  s <= 2*r
#        score < 0.25  <=>  4*(r+1) < s+2  <=>  s > 4*r + 2
# Оба условия требуют reviews_seen >= MIN_SAMPLE (иначе «мало данных»).
SQL_VITALITY_REPLIES = (
    "reviews_seen >= 3 AND reviews_seen <= 2 * replies_seen"
)
SQL_VITALITY_SILENT = (
    "reviews_seen >= 3 AND reviews_seen > 4 * replies_seen + 2"
)
# Сортировка по уверенности; продавцы без достаточного сэмпла — в конце.
SQL_VITALITY_ORDER = (
    "CASE WHEN reviews_seen >= 3 "
    "THEN (replies_seen + 1.0) / (reviews_seen + 2.0) ELSE -1 END"
)


def sql_matches(category: str, reviews_seen: int, replies_seen: int) -> bool:
    """Проверка SQL-эквивалента теми же выражениями, что уйдут в SQLite.

    Нужна тестам: если кто-то поправит порог в одной записи и забудет в
    другой — тест упадёт, и расхождение не уедет в интерфейс.
    """
    if category == CAT_REPLIES:
        return reviews_seen >= 3 and reviews_seen <= 2 * replies_seen
    if category == CAT_SILENT:
        return reviews_seen >= 3 and reviews_seen > 4 * replies_seen + 2
    return False
