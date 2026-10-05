"""Lead Intelligence: оценка перспективности продавцов поверх существующего
скоринга (v1.17, раздел 11 ТЗ).

Принципы:
* НЕТ второй независимой формулы. Commercial Score собирается из ТЕХ ЖЕ
  слагаемых, что и `score` в src/outreach/scoring.py (логарифмы объёмов,
  срок, рейтинг), плюс ассортимент, целевые категории и полнота данных.
* Два направления отдельно: Commercial (потенциал магазина) и Engagement
  (вероятность интереса по фактам коммуникаций из журнала).
* Итог объясним: каждый компонент возвращает причины словами — менеджер
  видит, почему лид наверху.
* Честность: пока коммуникаций мало, Engagement помечается низкой
  уверенностью; предположение не выдаётся за достоверную вероятность.
* Градация A/B/C/D/Unqualified — пороги из конфига
  outreach_data/lead_config.json, ручная смена фиксирует причину.
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Публичные категории ответов (раздел 11.6). Внутренние ключи classify.py
# остаются как есть — это их отображение в термины ТЗ; 'unknown' требует
# ручной проверки (handover менеджеру).
PUBLIC_REPLY_CATEGORIES = {
    "positive": "Positive",
    "interested": "Positive",
    "needs_info": "Question",
    "question": "Question",
    "neutral": "Neutral",
    "negotiation": "Negotiation",
    "negative": "Refusal",
    "refusal": "Refusal",
    "optout": "Opt-out",
    "other": "Unknown",
}
PUBLIC_CATEGORIES = ("Positive", "Question", "Negotiation",
                     "Neutral", "Refusal", "Opt-out", "Unknown")

# Вклад категории ответа в Engagement Score (объяснимая шкала 0..100 с
# уровнем уверенности, а НЕ «достоверная вероятность согласия»).
ENGAGEMENT_CATEGORY_WEIGHTS = {
    "interested": 30,
    "positive": 25,
    "needs_info": 15,
    "negotiation": 25,
    "question": 10,
    "neutral": 5,
    "other": 0,
    "negative": -40,
    "refusal": -40,
    "optout": 0,        # отказ от рассылки исключает продавца раньше скоринга
}
ENGAGEMENT_BASE = 50.0
ENGAGEMENT_CLAMP = (0.0, 100.0)

GRADES = ("A", "B", "C", "D", "U")
GRADE_LABELS = {
    "A": "высокий приоритет",
    "B": "перспективный",
    "C": "средний приоритет",
    "D": "низкий приоритет",
    "U": "не соответствует критериям",
}


class LeadConfig:
    """Пороги и веса Lead Intelligence. Файл outreach_data/lead_config.json
    факультативен: без него работают значения по умолчанию."""

    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.weights = {
            # Доли внутри Commercial Score. Порядок и смысл унаследованы от
            # score в scoring.py (объёмы+срок+рейтинг), новое — ассортимент,
            # категории, полнота/свежесть данных.
            "orders": float(data.get("orders", 0.25)),
            "reviews": float(data.get("reviews", 0.15)),
            "tenure": float(data.get("tenure", 0.15)),
            "rating": float(data.get("rating", 0.20)),
            "assortment": float(data.get("assortment", 0.10)),
            "category": float(data.get("category", 0.15)),
        }
        self.freshness_days = int(data.get("freshness_days", 14))
        self.freshness_penalty = float(data.get("freshness_penalty", 0.15))
        self.grade_thresholds = {
            # порог итогового приоритета (0..100) для каждой категории
            "A": float(data.get("grade_A", 75)),
            "B": float(data.get("grade_B", 60)),
            "C": float(data.get("grade_C", 40)),
        }
        self.priority_mix = float(data.get("priority_mix", 0.7))
        # 0.7 — доля Commercial в итоговом приоритете (остальное Engagement)
        self.target_categories = tuple(
            str(c).lower() for c in data.get("target_categories", ())
        )

    @classmethod
    def load(cls, path: Path) -> "LeadConfig":
        path = Path(path)
        if not path.exists():
            return cls()  # конфиг факультативен — умолчания без шума в логе
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return cls(data)
        except (OSError, ValueError) as exc:                 # noqa: BLE001
            logger.warning(f"Не прочёл {path}: {exc} — настройки по умолчанию")
        return cls()


def _log01(value: float, reference: float) -> float:
    """Логарифмическая нормировка 0..1 (та же логика сжатия, что в score)."""
    if value <= 0 or reference <= 0:
        return 0.0
    return min(math.log10(value + 1) / math.log10(reference + 1), 1.0)


def _data_completeness(row: dict) -> Tuple[float, List[str]]:
    """Доля заполненных полей, влияющих на оценку. Пустота данных — это
    не «плохой продавец», а низкая уверенность: учитываем множителем и
    объясняем словами."""
    fields = ("orders", "reviews", "months_on_ozon", "rating",
              "uscc", "province", "product_count")
    filled = 0
    missing: List[str] = []
    for name in fields:
        value = row.get(name) or 0
        if value:
            filled += 1
        else:
            missing.append(name)
    return filled / len(fields), missing


def _freshness_factor(row: dict, config: LeadConfig) -> Tuple[float, str]:
    """Свежесть метрик: старше freshness_days — штрафной множитель."""
    metrics_at = str(row.get("metrics_at") or "")
    if not metrics_at:
        return 1.0 - config.freshness_penalty, "метрики не собирались"
    try:
        age_days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(metrics_at)).days
    except ValueError:
        return 1.0 - config.freshness_penalty, "дата метрик не читается"
    if age_days <= config.freshness_days:
        return 1.0, ""
    return (max(1.0 - config.freshness_penalty, 0.0),
            f"метрики устарели ({age_days} дн.)")


def _category_match(row: dict, config: LeadConfig) -> Tuple[float, str]:
    """Совпадение ниши продавца с целевыми категориями из конфига.

    seller_categories — журнал замеров; берём последнюю известную нишу.
    Совпадение по подстроке пути категории (например «электроника» в
    «Электроника > Аудио > Наушники»).
    """
    if not config.target_categories:
        return 0.0, ""
    path = str(row.get("category_path") or row.get("category_leaf") or "").lower()
    if not path:
        return 0.0, "ниша не определена"
    for target in config.target_categories:
        if target in path:
            return 1.0, f"ниша целевая ({target})"
    return 0.0, f"ниша вне целевых ({path[:60]})"


def commercial_score(row: dict, config: LeadConfig) -> dict:
    """Коммерческий потенциал 0..100 и объяснение.

    Компоненты и веса — в LeadConfig; нормировки логарифмические, как в
    базовом score. Поломка одного поля не делает оценку нулевой — она
    занижает уверенность через полноту данных.
    """
    reasons: List[str] = []
    orders = float(row.get("orders") or 0)
    reviews = float(row.get("reviews") or 0)
    months = float(row.get("months_on_ozon") or 0)
    rating = float(row.get("rating") or 0)
    product_count = float(row.get("product_count") or 0)

    # Референсы — практические максимумы живой базы: 1 млн заказов,
    # 100k отзывов, 36 месяцев, 500 товаров. Логи сжимают разброс.
    components = {
        "orders": (_log01(orders, 1_000_000),
                   f"заказы {int(orders):,}".replace(",", " ")),
        "reviews": (_log01(reviews, 100_000), f"отзывы {int(reviews):,}".replace(",", " ")),
        "tenure": (min(months / 36.0, 1.0), f"на Ozon {int(months)} мес."),
        "rating": (min(rating / 5.0, 1.0),
                   (f"рейтинг {rating:g}" if rating else "рейтинга нет")),
        "assortment": (min(product_count / 500.0, 1.0),
                       f"товаров {int(product_count)}"),
    }
    category_component, category_reason = _category_match(row, config)
    components["category"] = (category_component, category_reason or "ниша не учитывается")

    total_weight = sum(config.weights.values()) or 1.0
    raw = 0.0
    for name, (component, _) in components.items():
        raw += config.weights.get(name, 0.0) * component
    raw = raw / total_weight

    completeness, missing = _data_completeness(row)
    freshness, freshness_reason = _freshness_factor(row, config)

    if orders >= 10_000:
        reasons.append("крупный объём заказов")
    if rating >= 4.5:
        reasons.append("высокий рейтинг")
    if rating and rating < 3.5:
        reasons.append(f"низкий рейтинг ({rating:g}) снижает перспективность")
    if months >= 24:
        reasons.append("магазин давно на площадке")
    if missing:
        reasons.append("нет данных: " + ", ".join(missing) + " — уверенность ниже")
    if freshness_reason:
        reasons.append(freshness_reason)
    if category_reason and "целевая" in category_reason:
        reasons.append(category_reason)

    score = round(100.0 * raw * completeness * freshness, 1)
    return {
        "commercial": score,
        "components": {name: round(component, 3) for name, (component, _) in components.items()},
        "completeness": round(completeness, 2),
        "reasons": reasons,
    }


def engagement_score(categories: Sequence[str]) -> dict:
    """Вероятностная ОЦЕНКА интереса по накопленным коммуникациям.

    Балл 0..100 от базы 50 и весов категорий ответов. Уверенность — от числа
    фактов: <2 ответа — «низкая», <5 — «средняя», дальше «высокая». Это
    предварительная оценка, а не вероятность согласия: данных мало —
    менеджер видит это прямо.
    """
    total = 0.0
    notes: List[str] = []
    counts: dict = {}
    for category in categories:
        weight = ENGAGEMENT_CATEGORY_WEIGHTS.get(str(category), 0)
        total += weight
        counts[category] = counts.get(category, 0) + 1
    for category, n in sorted(counts.items(), key=lambda kv: -abs(ENGAGEMENT_CATEGORY_WEIGHTS.get(kv[0], 0))):
        label = PUBLIC_REPLY_CATEGORIES.get(category, category)
        if n:
            notes.append(f"{label} ×{n}")
    score = round(max(ENGAGEMENT_CLAMP[0],
                      min(ENGAGEMENT_CLAMP[1], ENGAGEMENT_BASE + total)), 1)
    n = len(categories)
    confidence = ("низкая" if n < 2 else "средняя" if n < 5 else "высокая")
    if n == 0:
        notes = ["коммуникаций ещё не было — оценка нейтральная"]
    return {"engagement": score, "confidence": confidence,
            "events": n, "reasons": notes}


def final_priority(commercial: float, engagement: float,
                   config: LeadConfig) -> float:
    """Итоговый приоритет 0..100: коммерция с весом priority_mix, остальное —
    вовлечённость. Объяснимая свёртка, не «чёрный ящик»."""
    mix = min(max(config.priority_mix, 0.0), 1.0)
    return round(mix * (commercial or 0.0) + (1.0 - mix) * (engagement or 0.0), 1)


def grade_for(value: float, row: dict, config: LeadConfig) -> Tuple[str, str]:
    """Категория лида по порогам конфига. Unqualified — не по баллам, а по
    факту: продавец не подтверждён как китайский (эвристики не считаем)."""
    verified = str(row.get("verified_by") or "")
    if verified not in ("uscc", "warehouse"):
        return "U", "нет подтверждения китайского происхождения"
    if value >= config.grade_thresholds["A"]:
        return "A", f"приоритет {value} ≥ {config.grade_thresholds['A']:g}"
    if value >= config.grade_thresholds["B"]:
        return "B", f"приоритет {value} ≥ {config.grade_thresholds['B']:g}"
    if value >= config.grade_thresholds["C"]:
        return "C", f"приоритет {value} ≥ {config.grade_thresholds['C']:g}"
    return "D", f"приоритет {value} ниже порога C"


def score_lead(row: dict, reply_categories: Sequence[str],
               config: Optional[LeadConfig] = None) -> dict:
    """Полная объяснимая карточка оценки: commercial + engagement +
    итоговый приоритет + градация. Единая точка расчёта — дашборд, бэкфилл
    и тесты зовут только её."""
    config = config or LeadConfig()
    commercial = commercial_score(row, config)
    engagement = engagement_score(reply_categories)
    priority = final_priority(commercial["commercial"], engagement["engagement"], config)
    grade, grade_reason = grade_for(priority, row, config)
    reasons = commercial["reasons"] + engagement["reasons"]
    return {
        "commercial": commercial["commercial"],
        "engagement": engagement["engagement"],
        "engagement_confidence": engagement["confidence"],
        "engagement_events": engagement["events"],
        "priority": priority,
        "grade": grade,
        "grade_reason": grade_reason,
        "reasons": reasons,
        "components": commercial["components"],
        "completeness": commercial["completeness"],
    }
