"""
Мини-CRM для китайских продавцов Ozon.

Модули:
    store   — единое хранилище (SQLite): очередь рассылки + CRM-поля
    harvest — обход раздела «Товары из Китая» и сбор продавцов
    verify  — подтверждение страны: USCC из реквизитов, склад в Китае

Точка входа: `python run_harvest.py --help`
"""

from .store import (
    STATUS_LABELS,
    STATUS_NEW,
    STATUS_REFUSED,
    STATUS_REPLIED,
    STATUS_SENT,
    VERIFY_HEURISTIC,
    VERIFY_LABELS,
    VERIFY_NONE,
    VERIFY_USCC,
    VERIFY_WAREHOUSE,
    QueueStats,
    SellerStore,
)

__all__ = [
    "SellerStore",
    "QueueStats",
    "STATUS_LABELS",
    "STATUS_NEW",
    "STATUS_SENT",
    "STATUS_REPLIED",
    "STATUS_REFUSED",
    "VERIFY_LABELS",
    "VERIFY_NONE",
    "VERIFY_USCC",
    "VERIFY_WAREHOUSE",
    "VERIFY_HEURISTIC",
]
