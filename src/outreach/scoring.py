"""
Метрики и оценка продавцов Ozon.

Форматы строк взяты из реальных ответов модалки
`/modal/shop-in-shop-info?seller_id=...` (проверено на живых продавцах):

    orders_count    "702", "106 K", "33,9 K", "9 369"
    reviews_count   "260", "53,5 K", "3 338"
    working_time    "3 месяца", "1 год", "4 года"
    average_rating  "3,3 из 5", "4,7 из 5"

Числа приходят в русском формате: запятая как десятичный разделитель,
пробел (в том числе узкий неразрывный) как разделитель разрядов, суффиксы K/M.
Поэтому обычный int() на этих строках падает — нужен разбор ниже.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Пробелы-разделители разрядов, которые встречаются в ответах Ozon:
# обычный, неразрывный, узкий неразрывный, тонкий.
_SPACES = "\u0020\u00a0\u202f\u2009"

_SUFFIX_MULTIPLIER = {
    "": 1,
    "k": 1_000,
    "к": 1_000,
    "m": 1_000_000,
    "м": 1_000_000,
}

_COUNT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)([kmкм]?)$")
_ANY_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_TENURE_RE = re.compile(r"(\d+)\s*(год|лет|месяц)", re.IGNORECASE)


def _strip_spaces(raw: str) -> str:
    return "".join(ch for ch in str(raw) if ch not in _SPACES).strip()


def parse_count(raw: str) -> int:
    """«106 K» -> 106000, «33,9 K» -> 33900, «9 369» -> 9369, «702» -> 702.

    Возвращает 0, если разобрать не удалось — вызывающий код не должен
    падать из-за неожиданного формата от Ozon.
    """
    if raw is None:
        return 0
    text = _strip_spaces(raw).lower()
    if not text:
        return 0

    match = _COUNT_RE.match(text)
    if match:
        number = float(match.group(1).replace(",", "."))
        return int(number * _SUFFIX_MULTIPLIER[match.group(2)])

    # Запасной путь: берём первое число в строке (например «от 500 заказов»)
    match = _ANY_NUMBER_RE.search(text)
    if not match:
        logger.debug(f"Не разобрано число заказов/отзывов: {raw!r}")
        return 0
    return int(float(match.group(1).replace(",", ".")))


def parse_tenure_months(raw: str) -> int:
    """«3 месяца» -> 3, «1 год» -> 12, «4 года» -> 48, «5 лет» -> 60.

    Всё приводится к месяцам, чтобы сроки было можно сравнивать и складывать.
    """
    if not raw:
        return 0
    match = _TENURE_RE.search(_strip_spaces(raw))
    if not match:
        logger.debug(f"Не разобран срок работы: {raw!r}")
        return 0

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("месяц"):
        return amount
    if unit.startswith("год") or unit == "лет":
        return amount * 12
    return 0


def parse_rating(raw: str) -> float:
    """«4,9 из 5» -> 4.9, «4.7» -> 4.7, «нет оценок» -> 0.0."""
    if not raw:
        return 0.0
    match = _ANY_NUMBER_RE.search(str(raw))
    if not match:
        return 0.0
    return float(match.group(1).replace(",", "."))


@dataclass
class SellerMetrics:
    """Числовые метрики продавца, приведённые к сравнимому виду."""

    seller_id: str
    shop_name: str = ""
    company_name: str = ""
    orders: int = 0
    reviews: int = 0
    months_on_ozon: int = 0
    rating: float = 0.0
    is_chinese: bool = False
    province: str = ""
    uscc: str = ""
    sample_product_url: str = ""
    # CRM-поля, которые заполняет харвестер
    seller_profile_url: str = ""
    verified_by: str = "none"        # uscc / warehouse / heuristic / none
    registration_kind: str = "unknown"
    chinese_reasons: str = ""
    score: float = 0.0
    score_parts: dict = field(default_factory=dict)
    
    recent_rating: float = 0.0
    unanswered_reviews: int = 0
    unanswered_questions: int = 0
    pain_score: float = 0.0
    vitality_category: str = "unknown"

    @classmethod
    def from_raw(
        cls,
        seller_id: str,
        orders_count: str = "",
        reviews_count: str = "",
        working_time: str = "",
        average_rating: str = "",
        **extra,
    ) -> "SellerMetrics":
        """Собирает метрики из строк ровно в том виде, в каком их отдаёт Ozon."""
        return cls(
            seller_id=str(seller_id),
            orders=parse_count(orders_count),
            reviews=parse_count(reviews_count),
            months_on_ozon=parse_tenure_months(working_time),
            rating=parse_rating(average_rating),
            **extra,
        )


@dataclass(frozen=True)
class ScoreWeights:
    """Веса оценки. Логарифмы обязательны: разброс заказов от сотен до сотен
    тысяч, и без сжатия один крупный продавец перевесит весь остальной список.
    """

    orders: float = 1.0
    reviews: float = 0.6
    tenure: float = 0.05
    rating: float = 2.0
    overwhelmed: float = 1.5
    cost_saving: float = 1.0

    def as_dict(self) -> dict:
        return {
            "orders": self.orders,
            "reviews": self.reviews,
            "tenure": self.tenure,
            "rating": self.rating,
            "overwhelmed": self.overwhelmed,
            "cost_saving": self.cost_saving,
        }


DEFAULT_WEIGHTS = ScoreWeights()


def _log10(value: int) -> float:
    """log10(x+1) с защитой от нуля и отрицательных значений."""
    return math.log10(max(value, 0) + 1)


def score_seller(metrics: SellerMetrics, weights: ScoreWeights = DEFAULT_WEIGHTS) -> float:
    """Оценка «качества» продавца для сортировки очереди рассылки.

    Считает четыре слагаемых и складывает с весами. Возвращает score и
    складывает разбивку по слагаемым в metrics.score_parts — чтобы в отчёте
    было видно, чем именно продавец набрал балл.
    """
    orders_part = weights.orders * _log10(metrics.orders)
    reviews_part = weights.reviews * _log10(metrics.reviews)
    tenure_part = weights.tenure * metrics.months_on_ozon
    rating_part = weights.rating * metrics.rating
    overwhelmed_part = weights.overwhelmed * _log10(metrics.pain_score) if metrics.pain_score > 0 else 0.0
    cost_saving_part = weights.cost_saving * int(metrics.is_chinese)

    metrics.score_parts = {
        "orders": round(orders_part, 3),
        "reviews": round(reviews_part, 3),
        "tenure": round(tenure_part, 3),
        "rating": round(rating_part, 3),
        "overwhelmed": round(overwhelmed_part, 3),
        "cost_saving": round(cost_saving_part, 3),
    }
    metrics.score = round(orders_part + reviews_part + tenure_part + rating_part + overwhelmed_part + cost_saving_part, 3)
    return metrics.score


def score_all(
    sellers: Iterable[SellerMetrics],
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> List[SellerMetrics]:
    """Проставляет score всем продавцам. Порядок входа сохраняется."""
    result = list(sellers)
    for seller in result:
        score_seller(seller, weights)
    return result


def sort_sellers(
    sellers: Sequence[SellerMetrics],
    best_last: bool = True,
) -> List[SellerMetrics]:
    """Сортирует по оценке. best_last=True — лучший в конце списка.

    Именно этот режим нужен, когда список читают снизу вверх: тогда самые
    перспективные продавцы оказываются первыми в глазах читающего.
    """
    return sorted(sellers, key=lambda s: s.score, reverse=not best_last)


def filter_chinese(sellers: Iterable[SellerMetrics]) -> List[SellerMetrics]:
    """Оставляет только подтверждённых китайских продавцов.

    Опирается на флаг is_chinese, который проставляет классификатор: для
    подтверждения нужен USCC из модалки продавца либо китайская провинция
    склада из виджета доставки.
    """
    return [s for s in sellers if s.is_chinese]


def format_report(sellers: Sequence[SellerMetrics], top: Optional[int] = None) -> str:
    """Человекочитаемая таблица для консоли — проверить оценку перед рассылкой."""
    rows = list(sellers)[:top] if top else list(sellers)
    if not rows:
        return "Список пуст."

    header = f"{'score':>7}  {'заказы':>9}  {'отзывы':>9}  {'мес':>4}  {'рейтинг':>7}  магазин"
    lines = [header, "-" * len(header)]
    for s in rows:
        lines.append(
            f"{s.score:>7.2f}  {s.orders:>9,}  {s.reviews:>9,}  "
            f"{s.months_on_ozon:>4}  {s.rating:>7.1f}  {s.shop_name or s.seller_id}"
        )
    return "\n".join(lines)
