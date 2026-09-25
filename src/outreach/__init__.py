"""
Рассылка китайским продавцам Ozon.

Модули:
    scoring     — разбор метрик продавца и оценка для сортировки очереди
    queue_db    — SQLite-очередь с дедупликацией, стоп-листом и историей попыток
    templates   — шаблоны сообщений с персонализацией
    sender      — отправка через чат Ozon с ограничителями
    inspect_chat — одноразовая разведка разметки чата

Точка входа: `python run_outreach.py --help`
"""

from .scoring import (
    DEFAULT_WEIGHTS,
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
from .queue_db import OutreachQueue, QueueStats

__all__ = [
    "DEFAULT_WEIGHTS",
    "ScoreWeights",
    "SellerMetrics",
    "filter_chinese",
    "format_report",
    "parse_count",
    "parse_rating",
    "parse_tenure_months",
    "score_all",
    "sort_sellers",
    "OutreachQueue",
    "QueueStats",
]
