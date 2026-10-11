"""
Единое хранилище продавцов: и очередь рассылки, и мини-CRM.

Одна SQLite на весь проект — `data/sellers.sqlite3`. Раньше результаты жили
только в JSON внутри `output/`, и это давало три проблемы: при новом прогоне
дашборд терял данные, папку с результатами удалял `_delete_output_folder`, а
дедупликации между прогонами не было вообще. База решает всё это сразу.

Класс `SellerStore` совместим по методам с прежним `OutreachQueue`
(`claim_next`, `mark_sent`, `mark_failed`, `sent_last_24h` и так далее), поэтому
отправщик продолжает работать без правок, а CRM получает свои методы:
заметки, ручную смену статуса, фильтры и пагинацию.
"""

import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..outreach.scoring import SellerMetrics
from .vitality import SQL_VITALITY_ORDER, SQL_VITALITY_REPLIES, SQL_VITALITY_SILENT

logger = logging.getLogger(__name__)

# --- Статусы продавца -----------------------------------------------------
# Совместимы со старой очередью рассылки: sent/sending/failed/new/skipped/
# opted_out использовались отправщиком, а contacted/replied/refused — это
# ручные пометки для CRM.
STATUS_NEW = "new"                # ждёт отправки
STATUS_SENDING = "sending"        # взяли в работу прямо сейчас
STATUS_SENT = "sent"              # сообщение отправлено ("написал")
STATUS_REPLIED = "replied"        # ответил
STATUS_REFUSED = "refused"        # отказ
STATUS_FAILED = "failed"          # попытка не удалась
STATUS_SKIPPED = "skipped"        # исключён вручную
STATUS_OPTED_OUT = "opted_out"    # попросил не писать

TERMINAL_STATUSES = (STATUS_SENT, STATUS_SKIPPED, STATUS_OPTED_OUT, STATUS_REFUSED)

STATUS_LABELS = {
    STATUS_NEW: "не написал",
    STATUS_SENDING: "отправляется",
    STATUS_SENT: "написал",
    STATUS_REPLIED: "ответил",
    STATUS_REFUSED: "отказ",
    STATUS_FAILED: "ошибка",
    STATUS_SKIPPED: "пропущен",
    STATUS_OPTED_OUT: "не писать",
}

# --- Категории ответов ------------------------------------------------------
# Ставятся автоматически при появлении ответа (src/crm/classify.py) и могут
# быть поправлены вручную в дашборде. Ключи английские — как у статусов;
# в интерфейсе и отчётах показываются русские метки. «НЕ ОТВЕТИЛ» в sellers
# не хранится: это производное состояние (писали, а входящих нет), его
# report/списки выводят сами.
REPLY_POSITIVE = "positive"      # ПОЗИТИВНЫЙ: рад, спасибо, хорошо
REPLY_NEGATIVE = "negative"      # НЕГАТИВНЫЙ: не интересно, не нужно
REPLY_NEUTRAL = "neutral"        # НЕЙТРАЛЬНЫЙ: ок, понял, привет
REPLY_INTERESTED = "interested"  # ЗАИНТЕРЕСОВАН: давайте обсудим условия
REPLY_NEEDS_INFO = "needs_info"  # НУЖНА ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ: расскажите подробнее
REPLY_OTHER = "other"            # ДРУГОЕ: не получилось отнести к категории

REPLY_CATEGORY_LABELS = {
    REPLY_POSITIVE: "ПОЗИТИВНЫЙ",
    REPLY_NEGATIVE: "НЕГАТИВНЫЙ",
    REPLY_NEUTRAL: "НЕЙТРАЛЬНЫЙ",
    REPLY_INTERESTED: "ЗАИНТЕРЕСОВАН",
    REPLY_NEEDS_INFO: "НУЖНА ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ",
    REPLY_OTHER: "ДРУГОЕ",
}

# «Заинтересованные» для отдельного списка: положительный или потенциально
# положительный ответ. Эти продавцы не должны теряться в общей базе.
INTERESTED_CATEGORIES = (REPLY_POSITIVE, REPLY_INTERESTED, REPLY_NEEDS_INFO)

# --- Уровни достоверности ------------------------------------------------
# Насколько надёжно подтверждено, что продавец китайский. В дашборде по
# умолчанию показываются только первые два — эвристики дают ложные плюсы.
VERIFY_USCC = "uscc"            # 18-значный код из реквизитов — абсолютный
VERIFY_WAREHOUSE = "warehouse"  # склад продавца в Китае (виджет доставки)
VERIFY_HEURISTIC = "heuristic"  # эвристики по названию — НЕ доказательство
VERIFY_NONE = "none"

VERIFY_LABELS = {
    VERIFY_USCC: "USCC (подтверждён)",
    VERIFY_WAREHOUSE: "склад в Китае",
    VERIFY_HEURISTIC: "эвристика",
    VERIFY_NONE: "не проверен",
}

# Колонки CRM, добавляемые миграцией к существующей базе очереди
CRM_COLUMNS = {
    "notes": "TEXT NOT NULL DEFAULT ''",
    "seller_profile_url": "TEXT NOT NULL DEFAULT ''",
    "verified_by": "TEXT NOT NULL DEFAULT 'none'",
    "uscc": "TEXT NOT NULL DEFAULT ''",
    "province": "TEXT NOT NULL DEFAULT ''",
    "registration_kind": "TEXT NOT NULL DEFAULT 'unknown'",
    "address": "TEXT NOT NULL DEFAULT ''",
    "first_seen_at": "TEXT NOT NULL DEFAULT ''",
    "last_seen_at": "TEXT NOT NULL DEFAULT ''",
    "product_count": "INTEGER NOT NULL DEFAULT 0",
    "chinese_reasons": "TEXT NOT NULL DEFAULT ''",
    # Когда сообщение РЕАЛЬНО ушло. Нужна отдельно от updated_at: тот меняется
    # от любой правки (заметка, смена статуса), и дневной лимит считался по
    # нему — отметка «ответил» съедала лимит и сокращала отправки.
    "sent_at": "TEXT NOT NULL DEFAULT ''",
    # Когда в последний раз обновлялись метрики (модалка продавца). Отдельно
    # от updated_at: инкрементальный сбор пропускает известных продавцов и
    # возвращается к их модалкам только раз в N дней (флаг --refresh-days).
    "metrics_at": "TEXT NOT NULL DEFAULT ''",
    # Стадия сделки для тех, кто ответил: '', 'negotiation', 'deal', 'dead'.
    # Отдельная ось CRM: статус — про рассылку, стадия — про сделку.
    "stage": "TEXT NOT NULL DEFAULT ''",
    # Категория ПОСЛЕДНЕГО ответа продавца. Полная история категорий —
    # в messages.category у каждой входящей записи.
    "reply_category": "TEXT NOT NULL DEFAULT ''",
    # Когда получен последний ответ (сообщение result='received').
    "reply_at": "TEXT NOT NULL DEFAULT ''",
    # Когда продавец передан менеджеру. Пусто = бот ещё ведёт.
    "handed_to_manager_at": "TEXT NOT NULL DEFAULT ''",
    # Живость продавца (src/crm/review_sample.py): сколько отзывов рассмотрено
    # в сэмплах с карточек и у скольких был ответ магазина. Считается
    # кумулятивно — сэмплы с разных карточек одного продавца складываются.
    "reviews_seen": "INTEGER NOT NULL DEFAULT 0",
    "replies_seen": "INTEGER NOT NULL DEFAULT 0",
    # Когда сэмпл отзывов приходил последний раз — свежесть данных о живости.
    "reviews_checked_at": "TEXT NOT NULL DEFAULT ''",
    "recent_rating": "REAL NOT NULL DEFAULT 0.0",
    "unanswered_reviews": "INTEGER NOT NULL DEFAULT 0",
    "unanswered_questions": "INTEGER NOT NULL DEFAULT 0",
    "pain_score": "REAL NOT NULL DEFAULT 0.0",
    "vitality_category": "TEXT NOT NULL DEFAULT 'unknown'",
}

# Журнал отправок. В sellers.message_sent хранится только последнее сообщение
# и оно перезаписывается, поэтому «что писали и что было на прошлой попытке»
# узнать было нельзя. result='received' — входящие от продавца: текст хранится
# ЦЕЛИКОМ, без обрезки и без служебных префиксов (главное требование — ни один
# ответ не должен теряться); sent_at у входящих — время получения.
MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id   TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT 'sent',
    error       TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    template_name TEXT NOT NULL DEFAULT '',
    sender_account_id TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    experiment_id TEXT NOT NULL DEFAULT '',
    platform_message_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_seller ON messages(seller_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_result ON messages(result, seller_id);
"""

SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS sellers (
    seller_id           TEXT PRIMARY KEY,
    shop_name           TEXT NOT NULL DEFAULT '',
    company_name        TEXT NOT NULL DEFAULT '',
    uscc                TEXT NOT NULL DEFAULT '',
    wechat              TEXT NOT NULL DEFAULT '',
    email               TEXT NOT NULL DEFAULT '',
    phone               TEXT NOT NULL DEFAULT '',
    province            TEXT NOT NULL DEFAULT '',
    orders              INTEGER NOT NULL DEFAULT 0,
    reviews             INTEGER NOT NULL DEFAULT 0,
    months_on_ozon      INTEGER NOT NULL DEFAULT 0,
    rating              REAL NOT NULL DEFAULT 0,
    score               REAL NOT NULL DEFAULT 0,
    is_chinese          INTEGER NOT NULL DEFAULT 0,
    sample_product_url  TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'new',
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     TEXT,
    last_error          TEXT NOT NULL DEFAULT '',
    message_sent        TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    notes               TEXT NOT NULL DEFAULT '',
    seller_profile_url  TEXT NOT NULL DEFAULT '',
    verified_by         TEXT NOT NULL DEFAULT 'none',
    registration_kind   TEXT NOT NULL DEFAULT 'unknown',
    address             TEXT NOT NULL DEFAULT '',
    first_seen_at       TEXT NOT NULL DEFAULT '',
    last_seen_at        TEXT NOT NULL DEFAULT '',
    product_count       INTEGER NOT NULL DEFAULT 0,
    chinese_reasons     TEXT NOT NULL DEFAULT '',
    metrics_at          TEXT NOT NULL DEFAULT '',
    reply_category      TEXT NOT NULL DEFAULT '',
    reply_at            TEXT NOT NULL DEFAULT '',
    handed_to_manager_at TEXT NOT NULL DEFAULT '',
    reviews_seen        INTEGER NOT NULL DEFAULT 0,
    replies_seen        INTEGER NOT NULL DEFAULT 0,
    reviews_checked_at  TEXT NOT NULL DEFAULT '',
    recent_rating       REAL NOT NULL DEFAULT 0.0,
    unanswered_reviews  INTEGER NOT NULL DEFAULT 0,
    unanswered_questions INTEGER NOT NULL DEFAULT 0,
    pain_score          REAL NOT NULL DEFAULT 0.0,
    vitality_category   TEXT NOT NULL DEFAULT 'unknown',
    priority            INTEGER NOT NULL DEFAULT 0
);
"""

# Кэш «товар → продавец». Определение продавца по карточке товара — самый
# дорогой запрос во всём сборе (по одному на товар), а связка «товар этого
# раздела продаёт этот продавец» меняется редко. Повторный проход берёт
# продавца из кэша и не открывает карточку заново (протухание 14 дней:
# товар, исчезнувший из раздела, перестаёт считаться свежим).
PRODUCTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS seller_products (
    article        TEXT PRIMARY KEY,
    seller_id      TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seller_products_seen ON seller_products(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_seller_products_seller ON seller_products(seller_id);
"""

# Нишевые категории — ЖУРНАЛ измерений, не справочник: каждая снятая категория
# дописывается новой строкой, существующие данные никогда не переписываются
# (правило «не потерять правду»). Актуальная категория продавца — строка с
# максимальным id. Таблица аддитивная: ничего из существующего не меняет.

EXPERIMENTS_SCHEMA = '''
CREATE TABLE IF NOT EXISTS experiments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    criteria       TEXT NOT NULL DEFAULT '{}',
    variants       TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS experiment_sellers (
    experiment_id  INTEGER NOT NULL,
    seller_id      TEXT NOT NULL,
    variant        TEXT NOT NULL,
    assigned_at    TEXT NOT NULL,
    PRIMARY KEY(experiment_id, seller_id)
);
'''

FUNNEL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS funnel_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    event_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funnel_seller ON funnel_transitions(seller_id);
'''

CATEGORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS seller_categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id      TEXT NOT NULL,
    article        TEXT NOT NULL DEFAULT '',
    category_path  TEXT NOT NULL DEFAULT '',
    category_leaf  TEXT NOT NULL DEFAULT '',
    category_id    TEXT NOT NULL DEFAULT '',
    collected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seller_categories_seller ON seller_categories(seller_id, id);
CREATE INDEX IF NOT EXISTS idx_seller_categories_leaf ON seller_categories(category_leaf);
"""

# Индексы создаются ПОСЛЕ миграции: на старой базе очереди колонки verified_by
# ещё нет, и CREATE INDEX по ней упал бы.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sellers_status_score ON sellers(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_sellers_verified ON sellers(verified_by, score DESC);
"""

# Индекс по sent_at создаём отдельно: на старой базе колонки ещё нет,
# её добавляет миграция.
SCHEMA_INDEXES_AFTER_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_sellers_sent_at ON sellers(sent_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class QueueStats:
    total: int = 0
    by_status: dict = None
    sent_last_24h: int = 0
    by_verification: dict = None

    def __post_init__(self):
        self.by_status = self.by_status or {}
        self.by_verification = self.by_verification or {}

    def as_text(self) -> str:
        statuses = ", ".join(f"{k}={v}" for k, v in sorted(self.by_status.items()))
        verify = ", ".join(f"{k}={v}" for k, v in sorted(self.by_verification.items()))
        return (
            f"всего={self.total} ({statuses or 'пусто'}), "
            f"достоверность: {verify or 'нет'}, "
            f"за сутки отправлено={self.sent_last_24h}"
        )


# Поля выборки для дашборда и экспорта
SELECT_FIELDS = (
    "seller_id, shop_name, company_name, uscc, province, registration_kind, address, "
    "orders, reviews, months_on_ozon, rating, score, is_chinese, verified_by, "
    "seller_profile_url, sample_product_url, status, notes, attempts, last_attempt_at, "
    "last_error, message_sent, first_seen_at, last_seen_at, product_count, chinese_reasons, "
    "sent_at, metrics_at, stage, reply_category, reply_at, handed_to_manager_at, "
    "reviews_seen, replies_seen, reviews_checked_at, "
    "recent_rating, unanswered_reviews, unanswered_questions, pain_score, vitality_category"
)


class SellerStore:
    """Хранилище продавцов. Один файл, один процесс-писатель."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # WAL: дашборд читает базу, пока харвестер пишет. В обычном режиме
            # журнала это давало бы «database is locked» на чтении. Плюс WAL
            # переживает аварийное завершение процесса без порчи файла.
            conn.execute("PRAGMA journal_mode=WAL")
            # synchronous=NORMAL — безопасный компромисс для WAL: данные не
            # теряются при падении процесса, только при потере питания машины.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_TABLE)
            # Сначала досыпаем недостающие колонки, и только потом индексы:
            # на базе старой очереди колонки verified_by ещё нет.
            self._migrate(conn)
            conn.executescript(SCHEMA_INDEXES)
            conn.executescript(SCHEMA_INDEXES_AFTER_MIGRATION)
            conn.executescript(MESSAGES_SCHEMA)
            self._migrate_messages(conn)
            conn.executescript(PRODUCTS_SCHEMA)
            conn.executescript(CATEGORIES_SCHEMA)
            conn.executescript(EXPERIMENTS_SCHEMA)
            conn.executescript(FUNNEL_SCHEMA)

    @staticmethod
    def _migrate_messages(conn: sqlite3.Connection) -> None:
        """Добавляет недостающие колонки в журнал сообщений."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        extra_cols = {
            "category": "TEXT NOT NULL DEFAULT ''",
            "template_name": "TEXT NOT NULL DEFAULT ''",
            "sender_account_id": "TEXT NOT NULL DEFAULT ''",
            "campaign_id": "TEXT NOT NULL DEFAULT ''",
            "experiment_id": "TEXT NOT NULL DEFAULT ''",
            "platform_message_id": "TEXT NOT NULL DEFAULT ''",
        }
        for col, col_def in extra_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {col_def}")
                logger.info(f"Миграция: добавлена колонка messages.{col}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Добавляет недостающие CRM-колонки к базе, созданной старой версией.

        Существующая очередь рассылки не должна сломаться: статусы и история
        отправок сохраняются, новые поля просто появляются пустыми.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(sellers)")}
        for column, definition in CRM_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE sellers ADD COLUMN {column} {definition}")
                logger.info(f"Миграция: добавлена колонка sellers.{column}")
                # metrics_at появляется один раз в истории базы здесь же и
                # бэкфиллится: этим же прогоном метрики уже собирались, и без
                # отметки первый же инкрементальный сбор перечитывал модалки
                # всех известных продавцов.
                if column == "metrics_at":
                    conn.execute(
                        "UPDATE sellers SET metrics_at = updated_at WHERE metrics_at = ''"
                    )

    # --- Наполнение -------------------------------------------------------

    def add_sellers(self, sellers: Sequence[SellerMetrics]) -> int:
        """Добавляет продавцов, обновляя метрики у уже известных.

        Статус, заметки и историю попыток НЕ перезаписывает: повторный импорт
        не должен возвращать в рассылку тех, кому уже писали или кто отказался.
        """
        if not sellers:
            return 0

        added = 0
        now = _now()
        with self._connect() as conn:
            for s in sellers:
                exists = conn.execute(
                    "SELECT seller_id FROM sellers WHERE seller_id = ?", (s.seller_id,)
                ).fetchone()

                verified = getattr(s, "verified_by", VERIFY_NONE) or VERIFY_NONE
                if exists:
                    # Защита от обнуления: транспортный сбой при чтении модалки
                    # даёт пустые метрики, и UPDATE с пустотой «замораживал» их
                    # на refresh_days (аудит: 21 подтверждённых с обнулённым
                    # uscc). Пустое новое значение не затирает непустое старое.
                    # verified_by двигается только вверх: heuristic/none не
                    # понижают uscc/warehouse (легаси-путь audited отдельно).
                    conn.execute(
                        """UPDATE sellers SET
                               shop_name = ?, company_name = ?,
                               uscc = CASE WHEN ? != '' THEN ? ELSE uscc END,
                               province = CASE WHEN ? != '' THEN ? ELSE province END,
                               orders = CASE WHEN ? > 0 THEN ? ELSE orders END,
                               reviews = CASE WHEN ? > 0 THEN ? ELSE reviews END,
                               months_on_ozon = CASE WHEN ? > 0 THEN ? ELSE months_on_ozon END,
                               rating = CASE WHEN ? > 0 THEN ? ELSE rating END,
                               score = CASE WHEN ? > 0 THEN ? ELSE score END,
                               is_chinese = CASE WHEN ? != 0 THEN ? ELSE is_chinese END,
                               verified_by = CASE
                                   WHEN ? IN ('uscc', 'warehouse') THEN ?
                                   WHEN verified_by IN ('uscc', 'warehouse') THEN verified_by
                                   ELSE ? END,
                               seller_profile_url = CASE WHEN seller_profile_url = '' THEN ? ELSE seller_profile_url END,
                               sample_product_url = CASE WHEN sample_product_url = '' THEN ? ELSE sample_product_url END,
                               chinese_reasons = CASE WHEN ? != '' THEN ? ELSE chinese_reasons END,
                               last_seen_at = ?, updated_at = ?,
                               metrics_at = CASE WHEN ? != '' THEN ? ELSE metrics_at END,
                               recent_rating = CASE WHEN ? > 0 THEN ? ELSE recent_rating END,
                               unanswered_reviews = CASE WHEN ? >= 0 THEN ? ELSE unanswered_reviews END,
                               unanswered_questions = CASE WHEN ? >= 0 THEN ? ELSE unanswered_questions END,
                               pain_score = CASE WHEN ? > 0 THEN ? ELSE pain_score END,
                               vitality_category = CASE WHEN ? != 'unknown' THEN ? ELSE vitality_category END
                           WHERE seller_id = ?""",
                        (
                            s.shop_name, s.company_name,
                            s.uscc, s.uscc, s.province, s.province,
                            s.orders, s.orders, s.reviews, s.reviews,
                            s.months_on_ozon, s.months_on_ozon, s.rating, s.rating,
                            s.score, s.score, int(s.is_chinese), int(s.is_chinese),
                            verified, verified, verified,
                            getattr(s, "seller_profile_url", "") or "",
                            s.sample_product_url,
                            getattr(s, "chinese_reasons", "") or "",
                            getattr(s, "chinese_reasons", "") or "",
                            now, now,
                            # metrics_at двигаем только при реальных данных:
                            # пустой замок не должен «освежать» продавца
                            (now if (s.orders or s.reviews or s.uscc) else ""), now,
                            getattr(s, "recent_rating", 0.0), getattr(s, "recent_rating", 0.0),
                            getattr(s, "unanswered_reviews", 0), getattr(s, "unanswered_reviews", 0),
                            getattr(s, "unanswered_questions", 0), getattr(s, "unanswered_questions", 0),
                            getattr(s, "pain_score", 0.0), getattr(s, "pain_score", 0.0),
                            getattr(s, "vitality_category", "unknown"), getattr(s, "vitality_category", "unknown"),
                            s.seller_id,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sellers (
                               seller_id, shop_name, company_name, uscc, province,
                               orders, reviews, months_on_ozon, rating, score,
                               is_chinese, verified_by, registration_kind,
                               seller_profile_url, sample_product_url, chinese_reasons,
                               status, first_seen_at, last_seen_at, created_at, updated_at,
                               metrics_at, recent_rating, unanswered_reviews,
                               unanswered_questions, pain_score, vitality_category
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            s.seller_id, s.shop_name, s.company_name, s.uscc, s.province,
                            s.orders, s.reviews, s.months_on_ozon, s.rating, s.score,
                            int(s.is_chinese), verified,
                            getattr(s, "registration_kind", "unknown") or "unknown",
                            getattr(s, "seller_profile_url", "") or "",
                            s.sample_product_url,
                            getattr(s, "chinese_reasons", "") or "",
                            STATUS_NEW, now, now, now, now, now,
                            getattr(s, "recent_rating", 0.0),
                            getattr(s, "unanswered_reviews", 0),
                            getattr(s, "unanswered_questions", 0),
                            getattr(s, "pain_score", 0.0),
                            getattr(s, "vitality_category", "unknown"),
                        ),
                    )
                    added += 1
        logger.info(f"Импорт: добавлено {added}, обновлено {len(sellers) - added}")
        return added

    # --- Выборка для дашборда --------------------------------------------

    @staticmethod
    def _build_filter(
        verified_only: bool = True,
        statuses: Optional[Sequence[str]] = None,
        provinces: Optional[Sequence[str]] = None,
        search: str = "",
        tiers: Optional[Sequence[str]] = None,
        reply_categories: Optional[Sequence[str]] = None,
        vitality: Optional[str] = None,
        niches: Optional[Sequence[str]] = None,
        min_orders: Optional[int] = None,
        max_orders: Optional[int] = None,
        min_reviews: Optional[int] = None,
        max_reviews: Optional[int] = None,
    ) -> Tuple[str, List]:
        """Собирает WHERE и параметры для выборки продавцов.

        Один построитель на список и на подсчёт: раньше условия дублировались,
        и `count_sellers` не знал про фильтр по статусу — «N из M» врало, как
        только фильтр появлялся.
        """
        where: List[str] = []
        params: List = []

        if tiers:
            where.append(f"verified_by IN ({','.join('?' * len(tiers))})")
            params.extend(tiers)
        elif verified_only:
            where.append("verified_by IN (?, ?)")
            params.extend([VERIFY_USCC, VERIFY_WAREHOUSE])

        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)

        if provinces:
            where.append(f"province IN ({','.join('?' * len(provinces))})")
            params.extend(provinces)

        if reply_categories:
            where.append(f"reply_category IN ({','.join('?' * len(reply_categories))})")
            params.extend(reply_categories)

        if min_orders is not None:
            where.append("orders >= ?")
            params.append(min_orders)
        if max_orders is not None:
            where.append("orders <= ?")
            params.append(max_orders)
        if min_reviews is not None:
            where.append("reviews >= ?")
            params.append(min_reviews)
        if max_reviews is not None:
            where.append("reviews <= ?")
            params.append(max_reviews)

        # Живость: SQL-эквиваленты порогов из src/crm/vitality.py (без
        # параметров: условия целочисленные, инъекции там неоткуда взяться).
        if vitality == "replies":
            where.append(f"({SQL_VITALITY_REPLIES})")
        elif vitality == "silent":
            where.append(f"({SQL_VITALITY_SILENT})")

        # Ниша: только по АКТУАЛЬНОМУ замеру категории продавца
        if niches:
            where.append(
                "EXISTS (SELECT 1 FROM seller_categories sc "
                "WHERE sc.seller_id = sellers.seller_id "
                "AND sc.id = (SELECT MAX(id) FROM seller_categories "
                "WHERE seller_id = sc.seller_id) "
                f"AND sc.category_leaf IN ({','.join('?' * len(niches))}))"
            )
            params.extend(niches)

        if search:
            where.append(
                "(shop_name LIKE ? OR company_name LIKE ? OR uscc LIKE ? "
                "OR notes LIKE ? OR province LIKE ? OR chinese_reasons LIKE ?)"
            )
            pattern = f"%{search}%"
            params.extend([pattern] * 6)

        return (" WHERE " + " AND ".join(where) if where else ""), params

    def list_sellers(
        self,
        verified_only: bool = True,
        statuses: Optional[Sequence[str]] = None,
        search: str = "",
        order_by: str = "score",
        descending: bool = True,
        limit: int = 100,
        offset: int = 0,
        include_unverified: bool = False,
        provinces: Optional[Sequence[str]] = None,
        tiers: Optional[Sequence[str]] = None,
        reply_categories: Optional[Sequence[str]] = None,
        vitality: Optional[str] = None,
        niches: Optional[Sequence[str]] = None,
        min_orders: Optional[int] = None,
        max_orders: Optional[int] = None,
        min_reviews: Optional[int] = None,
        max_reviews: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        """Список продавцов для дашборда.

        По умолчанию отдаёт только подтверждённых (USCC или склад в Китае) —
        эвристики дают ложные плюсы, и показывать их вперемешку нельзя.
        """
        allowed_order = {
            "score", "orders", "reviews", "months_on_ozon", "rating",
            "shop_name", "company_name", "province", "status", "last_seen_at",
            # Дата последнего входящего: сортировка «кто ответил»
            "last_reply_at",
            # Живость: по сглаженной доле ответов, без сэмпла — вниз списка
            "vitality",
        }
        order_by = order_by if order_by in allowed_order else "score"

        where, params = self._build_filter(
            verified_only=verified_only and not include_unverified,
            statuses=statuses,
            provinces=provinces,
            search=search,
            tiers=tiers,
            reply_categories=reply_categories,
            vitality=vitality,
            niches=niches,
            min_orders=min_orders,
            max_orders=max_orders,
            min_reviews=min_reviews,
            max_reviews=max_reviews,
        )
        if order_by == "last_reply_at":
            order_sql = "last_reply_at"
        elif order_by == "vitality":
            order_sql = SQL_VITALITY_ORDER
        else:
            order_sql = order_by
        # База в подзапросе: WHERE остаётся однозначным (без префиксов), а JOIN
        # с сообщениями добавляет last_reply_at поверх отфильтрованных строк.
        sql = (
            f"SELECT t.*, r.last_reply_at, c.category_leaf AS niche FROM "
            f"(SELECT {SELECT_FIELDS} FROM sellers{where}) t "
            f"LEFT JOIN (SELECT seller_id, MAX(sent_at) AS last_reply_at "
            f"FROM messages WHERE result = 'received' GROUP BY seller_id) r "
            f"ON r.seller_id = t.seller_id "
            f"LEFT JOIN (SELECT seller_id, category_leaf FROM seller_categories sc "
            f"WHERE sc.id = (SELECT MAX(id) FROM seller_categories "
            f"WHERE seller_id = sc.seller_id)) c "
            f"ON c.seller_id = t.seller_id "
            f"ORDER BY {order_sql} {'DESC' if descending else 'ASC'} "
            f"LIMIT ? OFFSET ?"
        )
        params.extend([max(1, limit), max(0, offset)])

        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def count_sellers(
        self,
        verified_only: bool = True,
        search: str = "",
        statuses: Optional[Sequence[str]] = None,
        provinces: Optional[Sequence[str]] = None,
        tiers: Optional[Sequence[str]] = None,
        reply_categories: Optional[Sequence[str]] = None,
        vitality: Optional[str] = None,
        niches: Optional[Sequence[str]] = None,
    ) -> int:
        """Число записей под теми же условиями, что и list_sellers."""
        where, params = self._build_filter(
            verified_only=verified_only,
            statuses=statuses,
            provinces=provinces,
            search=search,
            tiers=tiers,
            reply_categories=reply_categories,
            vitality=vitality,
            niches=niches,
        )
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM sellers{where}", params).fetchone()["n"])

    def province_facets(self) -> List[Tuple[str, int]]:
        """Провинции с числом продавцов — для выпадающего фильтра."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT province, COUNT(*) AS n FROM sellers "
                "WHERE province != '' AND verified_by IN (?, ?) "
                "GROUP BY province ORDER BY n DESC, province",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchall()
        return [(r["province"], r["n"]) for r in rows]

    def recent_stats(self) -> dict:
        """Сводка для шапки: сколько подтверждённых, когда собирали, когда бэкап."""
        import os
        from datetime import datetime as _dt

        with self._connect() as conn:
            verified = int(conn.execute(
                "SELECT COUNT(*) c FROM sellers WHERE verified_by IN (?, ?)",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["c"])
            rejected = int(conn.execute(
                "SELECT COUNT(*) c FROM sellers WHERE verified_by NOT IN (?, ?)",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["c"])
            last_seen = conn.execute("SELECT MAX(last_seen_at) m FROM sellers").fetchone()["m"] or ""
            last_province = conn.execute(
                "SELECT COUNT(DISTINCT province) c FROM sellers WHERE province != ''"
            ).fetchone()["c"]

        backups = sorted((self.db_path.parent / "backups").glob("sellers-*.sqlite3")) \
            if (self.db_path.parent / "backups").exists() else []

        return {
            "verified": verified,
            "rejected": rejected,
            "provinces": int(last_province),
            "last_seen_at": last_seen,
            "last_backup_at": backups[-1].stat().st_mtime if backups else None,
            "last_backup_name": backups[-1].name if backups else "",
            "db_size_bytes": os.path.getsize(self.db_path) if self.db_path.exists() else 0,
        }

    def same_company_counts(self) -> Dict[str, int]:
        """Сколько витрин у каждой компании (по USCC).

        Одна китайская компания держит несколько магазинов — писать ей трижды
        подряд не нужно, поэтому в интерфейсе показываем «ещё N витрин».
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT uscc, COUNT(*) n FROM sellers WHERE uscc != '' GROUP BY uscc"
            ).fetchall()
        return {r["uscc"]: int(r["n"]) for r in rows}

    def get_seller(self, seller_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                f"SELECT {SELECT_FIELDS} FROM sellers WHERE seller_id = ?", (seller_id,)
            ).fetchone()

    # --- Инкрементальный сбор ----------------------------------------------

    def sellers_meta(self) -> Dict[str, dict]:
        """Сводка по всем известным продавцам для повторных проходов раздела.

        Позволяет харвестеру не открывать модалку продавца, который уже
        проверен и чьи метрики ещё свежие (см. metrics_at).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seller_id, shop_name, verified_by, metrics_at "
                "FROM sellers"
            ).fetchall()
        return {r["seller_id"]: dict(r) for r in rows}

    def product_owners(self, articles: Sequence[str], max_age_days: int = 14) -> Dict[str, str]:
        """Кэш «товар → продавец»: какие из товаров уже встречались разделам.

        Возвращает только связки не старше max_age_days (товар, надолго
        пропавший из разделов, может сменить продавца) и только для продавцов,
        которые ещё есть в базе.
        """
        if not articles:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(
            timespec="seconds"
        )
        placeholders = ",".join("?" * len(articles))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT p.article, p.seller_id FROM seller_products p "
                f"JOIN sellers s ON s.seller_id = p.seller_id "
                f"WHERE p.article IN ({placeholders}) AND p.last_seen_at >= ?",
                [*articles, cutoff],
            ).fetchall()
        return {r["article"]: r["seller_id"] for r in rows}

    def touch_products(self, pairs: Sequence[Tuple[str, str]]) -> int:
        """Освежает кэш товаров: новые связки вставляет, старые продлевает.

        Возвращает число новых записей (первых встреч товара).
        """
        if not pairs:
            return 0
        now = _now()
        fresh = 0
        with self._connect() as conn:
            for article, seller_id in pairs:
                cur = conn.execute(
                    "UPDATE seller_products SET seller_id = ?, last_seen_at = ? "
                    "WHERE article = ?",
                    (seller_id, now, article),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT INTO seller_products "
                        "(article, seller_id, first_seen_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?)",
                        (article, seller_id, now, now),
                    )
                    fresh += 1
        return fresh

    def mark_seen(self, items: Sequence[Tuple[str, str]]) -> int:
        """Отмечает продавца встреченным в разделе, не трогая метрики.

        Используется для известных продавцов, чьи модалки в этом прогоне не
        перечитывались: last_seen_at и ссылка на товар обновляются, а статус,
        реквизиты и метрики остаются как были.
        Пара — (seller_id, product_url).
        """
        if not items:
            return 0
        now = _now()
        seen = 0
        with self._connect() as conn:
            for seller_id, product_url in items:
                cur = conn.execute(
                    "UPDATE sellers SET "
                    "last_seen_at = ?, updated_at = ?, "
                    "sample_product_url = CASE WHEN sample_product_url = '' THEN ? "
                    "ELSE sample_product_url END "
                    "WHERE seller_id = ?",
                    (now, now, product_url, seller_id),
                )
                seen += cur.rowcount
        return seen

    def record_review_sample(self, seller_id: str, reviews_seen: int, replies_seen: int) -> bool:
        """Прибавляет сэмпл отзывов к живости продавца (кумулятивно).

        Сэмплы приходят с карточек товаров во время сбора, продавец встречается
        в нескольких товарах и прогонах — поэтому именно прибавляем, а не
        перезаписываем. updated_at не трогаем: живость — фоновая метрика, а не
        правка продавца, и зря двигать «обновлён» не стоит. Возвращает True,
        если продавец найден (для незнакомых сэмпл бросается — записывать
        некуда, попадёт в следующий прогон).
        """
        if reviews_seen <= 0 and replies_seen <= 0:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET "
                "reviews_seen = reviews_seen + ?, "
                "replies_seen = replies_seen + ?, "
                "reviews_checked_at = ? "
                "WHERE seller_id = ?",
                (max(0, reviews_seen), max(0, replies_seen), _now(), seller_id),
            )
            found = cur.rowcount > 0
        if found and (reviews_seen or replies_seen):
            logger.debug(
                f"Живость {seller_id}: +{reviews_seen} отзывов, +{replies_seen} ответов"
            )
        return found

    def vitality_counts(self) -> dict:
        """Сколько подтверждённых продавцов в каждой категории живости.

        Считает по тем же SQL-порогам, что и фильтр таблицы, и по тому же
        множеству (USCC + склад) — чтобы «Обзор» не расходился с таблицей.
        """
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT CASE "
                f"  WHEN NOT ({SQL_VITALITY_REPLIES}) AND NOT ({SQL_VITALITY_SILENT}) "
                f"  THEN 'mixed' "
                f"  WHEN ({SQL_VITALITY_REPLIES}) THEN 'replies' "
                f"  ELSE 'silent' END AS cat, "
                f"COUNT(*) AS n FROM sellers WHERE verified_by IN (?, ?) "
                f"AND reviews_seen >= 3 GROUP BY cat",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchall()
            sampled = int(conn.execute(
                "SELECT COUNT(*) AS n FROM sellers "
                "WHERE verified_by IN (?, ?) AND reviews_seen >= 3",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["n"])
            verified = int(conn.execute(
                "SELECT COUNT(*) AS n FROM sellers WHERE verified_by IN (?, ?)",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["n"])
        counts = {r["cat"]: int(r["n"]) for r in rows}
        counts["sampled"] = sampled
        # «Мало данных» — все подтверждённые, по кому сэмпла ещё не хватает
        counts["unknown"] = max(0, verified - sampled)
        counts["verified"] = verified
        return counts

    # --- Нишевые категории: журнал измерений, без переписывания -------------

    def record_category(self, seller_id: str, article: str, category: dict) -> bool:
        """Дописывает замер категории продавца в журнал (только INSERT).

        Ни одна существующая строка не меняется: замер — новое измерение,
        а не правка. Актуальной считается запись с максимальным id.
        """
        if not category or not category.get("leaf"):
            return False
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO seller_categories "
                "(seller_id, article, category_path, category_leaf, category_id, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (seller_id, article or "", category.get("path") or "",
                 category["leaf"], str(category.get("category_id") or ""), _now()),
            )
        return True

    def seller_categories_map(self) -> Dict[str, dict]:
        """Актуальная категория каждого продавца (строка с максимальным id)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sc.seller_id, sc.category_path, sc.category_leaf "
                "FROM seller_categories sc "
                "WHERE sc.id = (SELECT MAX(id) FROM seller_categories "
                "WHERE seller_id = sc.seller_id)"
            ).fetchall()
        return {r["seller_id"]: dict(r) for r in rows}

    def categorized_seller_ids(self) -> set:
        """Продавцы, у которых уже есть хотя бы один замер категории."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT seller_id FROM seller_categories").fetchall()
        return {r["seller_id"] for r in rows}

    def niche_facets(self) -> List[tuple]:
        """Ниши с числом подтверждённых продавцов — для выпадающего фильтра."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sc.category_leaf AS leaf, COUNT(*) AS n "
                "FROM seller_categories sc "
                "JOIN sellers s ON s.seller_id = sc.seller_id "
                "WHERE s.verified_by IN (?, ?) "
                "AND sc.id = (SELECT MAX(id) FROM seller_categories "
                "WHERE seller_id = sc.seller_id) "
                "GROUP BY sc.category_leaf ORDER BY n DESC, leaf",
                (VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchall()
        return [(r["leaf"], int(r["n"])) for r in rows]

    def category_stats(self, limit: int = 12) -> List[dict]:
        """Ниши × активность: сколько продавцов и сколько из них ответили.

        Это ответ на вопрос владельца «какая категория продавцов активнее
        подключается»: доля ответивших по факту журнала сообщений.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sc.category_leaf AS leaf, "
                "COUNT(DISTINCT sc.seller_id) AS sellers, "
                "SUM(CASE WHEN EXISTS (SELECT 1 FROM messages m "
                "WHERE m.seller_id = sc.seller_id AND m.result = 'received') "
                "THEN 1 ELSE 0 END) AS replied "
                "FROM seller_categories sc "
                "JOIN sellers s ON s.seller_id = sc.seller_id "
                "WHERE s.verified_by IN (?, ?) "
                "AND sc.id = (SELECT MAX(id) FROM seller_categories "
                "WHERE seller_id = sc.seller_id) "
                "GROUP BY sc.category_leaf ORDER BY sellers DESC LIMIT ?",
                (VERIFY_USCC, VERIFY_WAREHOUSE, max(1, limit)),
            ).fetchall()
        return [{"leaf": r["leaf"], "sellers": int(r["sellers"]),
                 "replied": int(r["replied"] or 0)} for r in rows]

    # --- Правки из дашборда ----------------------------------------------

    def set_note(self, seller_id: str, note: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET notes = ?, updated_at = ? WHERE seller_id = ?",
                (note, _now(), seller_id),
            )
            return cur.rowcount > 0

    def delete_sellers(self, seller_ids: Sequence[str]) -> int:
        """Удаляет записи по идентификаторам. Возвращает число удалённых.

        Нужно для чистки неподтверждённых: они скрыты в интерфейсе, но лежат
        в файле базы и попадают в выгрузку «--all». Журналы чистятся в той же
        транзакции: сироты после удаления рисуют пустые строки в лентах и
        вечно ссылаются на несуществующего продавца (аудит).
        """
        if not seller_ids:
            return 0
        with self._connect() as conn:
            placeholders = ",".join("?" * len(seller_ids))
            cur = conn.execute(f"DELETE FROM sellers WHERE seller_id IN ({placeholders})",
                               list(seller_ids))
            deleted = cur.rowcount
            for table in ("messages", "seller_products", "seller_categories"):
                conn.execute(f"DELETE FROM {table} WHERE seller_id IN ({placeholders})",
                             list(seller_ids))
        logger.info(f"Удалено записей: {deleted}")
        return deleted

    def set_verification_reason(
        self, seller_id: str, verified_by: str, reason: str, province: Optional[str] = None
    ) -> bool:
        """Обновляет уровень достоверности и причину вердикта.

        Нужно при перепроверке: вторичные признаки (склад) слабее USCC, и по
        итогам повторной проверки продавца можно как подтвердить с обоснованием,
        так и понизить до «не подтверждён».
        """
        if verified_by not in VERIFY_LABELS:
            raise ValueError(f"Неизвестный уровень: {verified_by}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET verified_by = ?, chinese_reasons = ?, "
                "province = CASE WHEN ? != '' THEN ? ELSE province END, "
                "is_chinese = ?, updated_at = ? WHERE seller_id = ?",
                (verified_by, reason, province or "", province or "",
                 int(verified_by in (VERIFY_USCC, VERIFY_WAREHOUSE)), _now(), seller_id),
            )
            return cur.rowcount > 0

    def set_status(self, seller_id: str, status: str) -> bool:
        """Ручная смена статуса. Статус used рассылки меняется осознанно."""
        if status not in STATUS_LABELS:
            raise ValueError(f"Неизвестный статус: {status}. Доступны: {', '.join(STATUS_LABELS)}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? WHERE seller_id = ?",
                (status, _now(), seller_id),
            )
            return cur.rowcount > 0

    # --- Совместимость с очередью рассылки -------------------------------

    def claim_next(
        self,
        require_product_url: bool = False,
        order_fresh: bool = False,
        min_orders: Optional[int] = None,
        max_orders: Optional[int] = None,
        min_reviews: Optional[int] = None,
        max_reviews: Optional[int] = None,
    ) -> Optional[sqlite3.Row]:
        """Следующий продавец на отправку и пометка «в работе».

        Сортировка по score DESC: сначала самые крупные и долгоживущие.
        Отправляем только подтверждённых — эвристики не берём, чтобы не писать
        российским продавцам.

        Ссылка на товар больше не требуется: чат открывается по странице
        магазина (`/seller/{id}/`), достаточно самого seller_id.
        """
        with self._connect() as conn:
            where = "status = ? AND verified_by IN (?, ?)"
            params: List = [STATUS_NEW, VERIFY_USCC, VERIFY_WAREHOUSE]
            if require_product_url:
                where += " AND sample_product_url != ''"
            if min_orders is not None:
                where += " AND orders >= ?"
                params.append(min_orders)
            if max_orders is not None:
                where += " AND orders <= ?"
                params.append(max_orders)
            if min_reviews is not None:
                where += " AND reviews >= ?"
                params.append(min_reviews)
            if max_reviews is not None:
                where += " AND reviews <= ?"
                params.append(max_reviews)

            row = conn.execute(
                f"SELECT * FROM sellers WHERE {where} ORDER BY priority DESC, {'first_seen_at DESC' if order_fresh else 'score DESC'} LIMIT 1", params
            ).fetchone()
            if not row:
                return None

            conn.execute(
                "UPDATE sellers SET status = ?, attempts = attempts + 1, "
                "last_attempt_at = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_SENDING, _now(), _now(), row["seller_id"]),
            )
            logger.info(f"В работу: {row['shop_name'] or row['seller_id']} (score={row['score']:.2f})")
            return conn.execute(
                f"SELECT {SELECT_FIELDS} FROM sellers WHERE seller_id = ?", (row["seller_id"],)
            ).fetchone()

    def mark_sent(
        self,
        seller_id: str,
        message: str,
        template_name: str = "",
        sender_account_id: str = "",
        experiment_id: str = "",
        **kwargs,
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, message_sent = ?, last_error = '', "
                "sent_at = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_SENT, message, now, now, seller_id),
            )
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result, template_name, sender_account_id, experiment_id) "
                "VALUES (?, ?, ?, 'sent', ?, ?, ?)",
                (seller_id, message, now, template_name, sender_account_id, str(experiment_id or "")),
            )
        logger.info(f"Отправлено: {seller_id}")

    def log_message(
        self,
        seller_id: str,
        text: str,
        result: str,
        error: str = "",
        template_name: str = "",
        sender_account_id: str = "",
        campaign_id: str = "",
        experiment_id: str = "",
        platform_message_id: str = "",
        **kwargs,
    ) -> None:
        """Записывает попытку в журнал, не меняя статус продавца."""
        import json as _json
        now = _now()
        err_str = _json.dumps(str(error)[:300], ensure_ascii=False)[1:-1] if error else ""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result, error, template_name, "
                "sender_account_id, campaign_id, experiment_id, platform_message_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seller_id,
                    text,
                    now,
                    result,
                    err_str,
                    template_name,
                    sender_account_id,
                    str(campaign_id or ""),
                    str(experiment_id or ""),
                    str(platform_message_id or ""),
                ),
            )

    def find_seller_by_shop(self, shop_name: str) -> Optional[sqlite3.Row]:
        """Продавец по имени магазина (без учёта регистра).

        Нужно для проверки входящих: Ozon в списке чатов отдаёт имя магазина,
        а не seller_id. Если витрин с таким именем несколько — НЕ угадываем:
        ответ одной витрины, записанный другой, хуже нераспознанного чата
        (аудит: дубли имён в живой базе реальны — 'velora' ×3). Такой чат
        уходит в unmatched на ручной разбор.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seller_id, status, shop_name FROM sellers "
                "WHERE lower(shop_name) = lower(?) ORDER BY score DESC",
                (shop_name.strip(),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            logger.warning(
                f"Магазин {shop_name!r}: {len(rows)} витрин с этим именем — "
                "чат требует ручного разбора (не матчу по имени)")
            return None
        return rows[0]

    # --- CRM: стадии сделки -------------------------------------------------

    STAGE_LABELS = {
        "": "—",
        "negotiation": "переговоры",
        "deal": "сделка",
        "dead": "закрыли",
    }

    def set_stage(self, seller_id: str, stage: str) -> bool:
        """Стадия сделки: пусто / переговоры / сделка / закрыли."""
        if stage not in self.STAGE_LABELS:
            raise ValueError(f"Неизвестная стадия: {stage}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET stage = ?, updated_at = ? WHERE seller_id = ?",
                (stage, _now(), seller_id),
            )
        return cur.rowcount > 0

    def stage_counts(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT stage, COUNT(*) AS n FROM sellers WHERE stage != '' "
                "GROUP BY stage"
            ).fetchall()
        return {r["stage"]: int(r["n"]) for r in rows}

    # --- Коммуникации: ответы, категории, передача менеджеру ----------------
    # Требование проекта: ни один ответ продавца не должен теряться. Полный
    # текст живёт в messages (result='received') навсегда; на самом продавце
    # лежит только «текущее» — последняя категория и дата последнего ответа.

    def save_reply(
        self, seller_id: str, text: str, received_at: str = "",
        category: Optional[str] = None,
    ) -> str:
        """Сохраняет ответ продавца ЦЕЛИКОМ и помечает его ответившим.

        Текст пишется в журнал как есть — без префиксов и обрезки. Дата
        получения — received_at (присылает Ozon), иначе текущий момент.
        Категорию ставит классификатор, если не передана явно. Возвращает
        категорию.
        """
        from .classify import classify_reply

        text = (text or "").strip()
        if not text:
            raise ValueError("Пустой ответ нельзя сохранять: это либо потеря данных, либо ошибка вызова")
        when = received_at or _now()
        if category is None:
            category = classify_reply(text)
        elif category not in REPLY_CATEGORY_LABELS:
            raise ValueError(
                f"Неизвестная категория: {category}. Доступны: {', '.join(REPLY_CATEGORY_LABELS)}"
            )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result, category) "
                "VALUES (?, ?, ?, 'received', ?)",
                (seller_id, text, when, category),
            )
            # Статус «ответил» ставим только тем, у кого рассылка ещё жива:
            # стоп-лист не переписываем, остальным — можно.
            conn.execute(
                "UPDATE sellers SET reply_category = ?, reply_at = ?, updated_at = ?, "
                "status = CASE WHEN status = ? THEN status ELSE ? END "
                "WHERE seller_id = ?",
                (category, when, _now(), STATUS_OPTED_OUT, STATUS_REPLIED, seller_id),
            )
        return category

    def update_reply_text(self, seller_id: str, text: str, received_at: str = "") -> bool:
        """Обновляет ПОСЛЕДНИЙ входящий продавца полным текстом.

        Нужно дотягиванию задним числом: продавцы, помеченные «ответил» до
        появления полного разбора чата, хранят только превью из списка
        чатов. Перезаписываем только если новый текст ДЛИННЕЕ сохранённого:
        превью боится обрезки, а укорачивать уже сохранённое опасно.
        Категория пересчитывается, дата получения уточняется, если пришла.
        """
        from .classify import classify_reply

        text = (text or "").strip()
        if not text:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, text FROM messages "
                "WHERE seller_id = ? AND result = 'received' "
                "ORDER BY id DESC LIMIT 1",
                (seller_id,),
            ).fetchone()
            if not row or len(text) <= len((row["text"] or "").strip()):
                return False
            category = classify_reply(text)
            if received_at:
                conn.execute(
                    "UPDATE messages SET text = ?, category = ?, sent_at = ? WHERE id = ?",
                    (text, category, received_at, row["id"]),
                )
                conn.execute(
                    "UPDATE sellers SET reply_category = ?, reply_at = ?, updated_at = ? "
                    "WHERE seller_id = ?",
                    (category, received_at, _now(), seller_id),
                )
            else:
                conn.execute(
                    "UPDATE messages SET text = ?, category = ? WHERE id = ?",
                    (text, category, row["id"]),
                )
                conn.execute(
                    "UPDATE sellers SET reply_category = ?, updated_at = ? "
                    "WHERE seller_id = ?",
                    (category, _now(), seller_id),
                )
        return True

    def set_reply_category(self, seller_id: str, category: str) -> bool:
        """Ручная правка категории: и на продавце, и на его последнем ответе."""
        if category not in REPLY_CATEGORY_LABELS:
            raise ValueError(
                f"Неизвестная категория: {category}. Доступны: {', '.join(REPLY_CATEGORY_LABELS)}"
            )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET reply_category = ?, updated_at = ? WHERE seller_id = ?",
                (category, _now(), seller_id),
            )
            if not cur.rowcount:
                return False
            last = conn.execute(
                "SELECT id FROM messages WHERE seller_id = ? AND result = 'received' "
                "ORDER BY id DESC LIMIT 1",
                (seller_id,),
            ).fetchone()
            if last:
                conn.execute("UPDATE messages SET category = ? WHERE id = ?", (category, last["id"]))
        return True

    def hand_to_manager(self, seller_id: str) -> bool:
        """Передаёт продавца менеджеру.

        Никаких копий: продавец остаётся в общей базе, отметка времени в
        handed_to_manager_at отфильтровывает тех, кем уже занимается человек.
        Стадия автоматически становится «переговоры», если была пустой.
        """
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET handed_to_manager_at = ?, updated_at = ?, "
                "stage = CASE WHEN stage = '' THEN 'negotiation' ELSE stage END "
                "WHERE seller_id = ?",
                (now, now, seller_id),
            )
            handed = cur.rowcount > 0
        if handed:
            logger.info(f"Передан менеджеру: {seller_id}")
        return handed

    def communication_counts(self) -> dict:
        """Главные цифры рассылки по фактам из журнала сообщений.

        «Писали» и «ответили» считаем по messages, а не по статусу: статус
        меняется руками, журнал — факты. «Не ответили» = писали минус ответили.
        """
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) n FROM sellers").fetchone()["n"])
            messaged = int(conn.execute(
                "SELECT COUNT(DISTINCT seller_id) n FROM messages WHERE result = 'sent'"
            ).fetchone()["n"])
            replied = int(conn.execute(
                "SELECT COUNT(DISTINCT seller_id) n FROM messages WHERE result = 'received'"
            ).fetchone()["n"])
            no_reply = int(conn.execute(
                "SELECT COUNT(*) n FROM ("
                "  SELECT DISTINCT seller_id FROM messages WHERE result = 'sent' "
                "  EXCEPT SELECT DISTINCT seller_id FROM messages WHERE result = 'received')"
            ).fetchone()["n"])
            handed = int(conn.execute(
                "SELECT COUNT(*) n FROM sellers WHERE handed_to_manager_at != ''"
            ).fetchone()["n"])
        return {
            "total": total,
            "messaged": messaged,
            "no_reply": no_reply,
            "replied": replied,
            "handed": handed,
        }

    def category_counts(self) -> dict:
        """Сколько продавцов в каждой категории последнего ответа."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT reply_category, COUNT(*) AS n FROM sellers "
                "WHERE reply_category != '' GROUP BY reply_category"
            ).fetchall()
        return {r["reply_category"]: int(r["n"]) for r in rows}

    def comm_report_rows(
        self,
        replied: Optional[bool] = None,
        categories: Optional[Sequence[str]] = None,
        handed: Optional[bool] = None,
        messaged: Optional[bool] = None,
        limit: int = 2000,
    ) -> List[dict]:
        """Строки для отчётов по коммуникации: продавец + последний ответ.

        replied: True — только ответившие, False — только без входящих, None —
        без условия. messaged: True — только те, кому реально писали. categories —
        фильтр по категории последнего ответа, handed — по передаче менеджеру.
        Условие «ответил» берётся из журнала, поэтому старые ручные пометки
        статуса отчёты не искажают. «Не ответили» = messaged=True + replied=False:
        без этого в список попадали все, кому ещё не дошла очередь.
        """
        where: List[str] = []
        params: List = []
        if messaged is True:
            where.append(
                "EXISTS (SELECT 1 FROM messages m WHERE m.seller_id = s.seller_id "
                "AND m.result = 'sent')"
            )
        elif messaged is False:
            where.append(
                "NOT EXISTS (SELECT 1 FROM messages m WHERE m.seller_id = s.seller_id "
                "AND m.result = 'sent')"
            )
        if replied is True:
            where.append(
                "EXISTS (SELECT 1 FROM messages m WHERE m.seller_id = s.seller_id "
                "AND m.result = 'received')"
            )
        elif replied is False:
            where.append(
                "NOT EXISTS (SELECT 1 FROM messages m WHERE m.seller_id = s.seller_id "
                "AND m.result = 'received')"
            )
        if categories:
            where.append(f"s.reply_category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        if handed is True:
            where.append("s.handed_to_manager_at != ''")
        elif handed is False:
            where.append("s.handed_to_manager_at = ''")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, limit))
        sql = f"""SELECT s.seller_id, s.shop_name, s.company_name, s.province, s.status,
                s.stage, s.notes, s.seller_profile_url, s.score, s.uscc,
                s.reply_category, s.reply_at, s.handed_to_manager_at,
                s.sent_at AS last_sent_at, s.message_sent AS last_sent_text,
                (SELECT text FROM messages WHERE seller_id = s.seller_id
                  AND result = 'received' ORDER BY id DESC LIMIT 1) AS last_reply_text
            FROM sellers s{where_sql}
            ORDER BY COALESCE(NULLIF(s.reply_at, ''), s.sent_at) DESC, s.score DESC
            LIMIT ?"""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def interested_rows(self, limit: int = 2000) -> List[dict]:
        """Заинтересованные: положительный или потенциально положительный ответ.

        Отдельный список, ради которого всё и затевалось: эти продавцы не
        должны теряться в общей базе. Копий не создаём — фильтр по категории.
        """
        return self.comm_report_rows(
            replied=True, categories=INTERESTED_CATEGORIES, limit=limit
        )

    def recategorize(self, overwrite: bool = False) -> int:
        """Прогоняет классификатор по сохранённым ответам.

        Без overwrite правит только пустые категории (старые записи);
        с overwrite — пересчитывает все. Нужно после обновления правил:
        тексты ответов хранятся целиком, поэтому пересчитать можно всегда.
        Заодно чистит исторический префикс «[входящее …]», который раньше
        вклеивался в текст ответа: в тексте должен быть только сам ответ.
        """
        from .classify import classify_reply

        with self._connect() as conn:
            # Старый inbox вклеивал дату в текст — убираем, дата есть в sent_at
            for row in conn.execute(
                "SELECT id, text FROM messages WHERE result = 'received' "
                "AND text LIKE '[входящее%'"
            ).fetchall():
                cleaned = re.sub(r"^\[входящее[^\]]*\]\s*", "", row["text"])
                if cleaned:
                    conn.execute(
                        "UPDATE messages SET text = ? WHERE id = ?", (cleaned, row["id"])
                    )
            rows = conn.execute(
                "SELECT id, seller_id, text, category FROM messages "
                "WHERE result = 'received' ORDER BY id"
            ).fetchall()
            changed = 0
            for row in rows:
                if row["category"] and not overwrite:
                    continue
                category = classify_reply(row["text"])
                if category == row["category"]:
                    continue
                conn.execute(
                    "UPDATE messages SET category = ? WHERE id = ?", (category, row["id"])
                )
                changed += 1
            # Категория продавца — по его последнему ответу в журнале.
            conn.execute(
                "UPDATE sellers SET reply_category = COALESCE("
                "  (SELECT category FROM messages"
                "   WHERE seller_id = sellers.seller_id AND result = 'received'"
                "   ORDER BY id DESC LIMIT 1), '')"
            )
        if changed:
            logger.info(f"Перекатегоризовано ответов: {changed}")
        return changed

    def reminders(self, days: int = 3, limit: int = 12) -> List[dict]:
        """«Пора напомнить»: ответили и замолчали больше N дней.

        Свежесть — по последней записи в журнале сообщений (входящее или
        исходящее); кто на стадии «сделка» или «закрыли», не тревожим.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.seller_id, s.shop_name, s.stage, s.status, "
                "       COALESCE(m.last_at, s.updated_at) AS last_touch, "
                "       s.score "
                "FROM sellers s "
                "LEFT JOIN (SELECT seller_id, MAX(sent_at) AS last_at "
                "           FROM messages GROUP BY seller_id) m "
                "  ON m.seller_id = s.seller_id "
                "WHERE s.status = 'replied' "
                "  AND s.stage NOT IN ('deal', 'dead') "
                "  AND COALESCE(m.last_at, s.updated_at) < ? "
                "ORDER BY s.score DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def replied_export_rows(self, limit: int = 2000) -> List[dict]:
        """Ответившие со стадиями, категориями и последними сообщениями — для XLSX."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.seller_id, s.shop_name, s.company_name, s.province, "
                "       s.orders, s.reviews, s.rating, s.score, s.status, s.stage, "
                "       s.notes, s.seller_profile_url, s.reply_category, s.reply_at, "
                "       s.handed_to_manager_at, "
                "       (SELECT text FROM messages WHERE seller_id = s.seller_id "
                "         AND result = 'received' ORDER BY id DESC LIMIT 1) AS last_incoming, "
                "       (SELECT text FROM messages WHERE seller_id = s.seller_id "
                "         AND result = 'sent' ORDER BY id DESC LIMIT 1) AS last_outgoing "
                "FROM sellers s "
                "WHERE s.status = 'replied' OR s.stage != '' "
                "ORDER BY s.score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def message_history(self, seller_id: str, limit: int = 50) -> List[sqlite3.Row]:
        """История переписки с продавцом, свежие сверху."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT text, sent_at, result, error, category FROM messages "
                "WHERE seller_id = ? ORDER BY sent_at DESC, id DESC LIMIT ?",
                (seller_id, limit),
            ).fetchall()

    def status_counts(self) -> Dict[str, int]:
        """Сколько продавцов в каждом статусе — для обзора и диаграмм.

        Считаем по всем записям (не только подтверждённым): статусы живут и
        у отсеянных, и исключать их из статистики — путать тех, кому писали.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM sellers GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def sent_by_day(self, days: int = 14) -> List[dict]:
        """Сколько real-отправок было в каждый из последних дней.

        Даты в базе UTC; фронт показывает «сколько штук в день», а не часы,
        поэтому смещение часового пояса не критично.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT substr(sent_at, 1, 10) AS d, COUNT(*) AS n FROM messages "
                "WHERE result = 'sent' AND sent_at >= ? GROUP BY d ORDER BY d",
                (cutoff,),
            ).fetchall()
        return [{"date": r["d"], "count": int(r["n"])} for r in rows]

    def replies_recent(self, limit: int = 10) -> List[dict]:
        """Реальные ответы продавцов (входящие) — свежие сверху.

        Для карточки «Ответили» на Обзоре: видно КТО ответил и что написал,
        а не только цифра. Только result='received' — проверенные входящие
        из чатов, а не ручные отметки статуса.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.seller_id, m.text, m.sent_at, m.category, "
                "       COALESCE(s.shop_name, '') AS shop_name "
                "FROM messages m LEFT JOIN sellers s ON s.seller_id = m.seller_id "
                "WHERE m.result = 'received' "
                "ORDER BY m.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def received_by_day(self, days: int = 15) -> List[dict]:
        """Ответы продавцов по дням (result='received') — для графика."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT substr(sent_at, 1, 10) AS d, COUNT(*) AS n FROM messages "
                "WHERE result = 'received' AND sent_at >= ? GROUP BY d ORDER BY d",
                (cutoff,),
            ).fetchall()
        return [{"date": r["d"], "count": int(r["n"])} for r in rows]

    def recent_messages(self, limit: int = 12) -> List[dict]:
        """Последние отправки с названием магазина — для ленты в обзоре."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.seller_id, m.text, m.sent_at, m.result, m.error, "
                "       COALESCE(s.shop_name, '') AS shop_name "
                "FROM messages m LEFT JOIN sellers s ON s.seller_id = m.seller_id "
                "ORDER BY m.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def requeue_failed(self, seller_ids: Optional[Sequence[str]] = None) -> int:
        """Возвращает в очередь тех, у кого была ошибка.

        Раньше статус «ошибка» был тупиковым: ничего не возвращало продавца
        в очередь, и повтор был невозможен без ручной правки.
        """
        with self._connect() as conn:
            if seller_ids:
                placeholders = ",".join("?" * len(seller_ids))
                cur = conn.execute(
                    f"UPDATE sellers SET status = ?, updated_at = ? "
                    f"WHERE status IN (?, ?) AND seller_id IN ({placeholders})",
                    [STATUS_NEW, _now(), STATUS_FAILED, STATUS_SKIPPED, *seller_ids],
                )
            else:
                cur = conn.execute(
                    "UPDATE sellers SET status = ?, updated_at = ? WHERE status IN (?, ?)",
                    (STATUS_NEW, _now(), STATUS_FAILED, STATUS_SKIPPED),
                )
            count = cur.rowcount
        if count:
            logger.info(f"Возвращено в очередь: {count}")
        return count

    def mark_failed(self, seller_id: str, error: str, retryable: bool = True, **kwargs) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, last_error = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_FAILED if retryable else STATUS_SKIPPED, str(error)[:500], _now(), seller_id),
            )
        logger.warning(f"Не отправлено: {seller_id} — {str(error)[:200]}")

    def mark_opted_out(self, seller_id: str, **kwargs) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_OPTED_OUT, _now(), seller_id),
            )
        logger.info(f"В стоп-лист: {seller_id}")

    def mark_skipped(self, seller_id: str, reason: str = "", **kwargs) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, last_error = ?, updated_at = ? "
                "WHERE seller_id = ?",
                (STATUS_SKIPPED, (reason or "пропущен")[:500], _now(), seller_id),
            )

    def release_stale(self, older_than_minutes: int = 30) -> int:
        """Возвращает в очередь зависшие «в работе» после аварийного выхода."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? WHERE status = ? AND last_attempt_at < ?",
                (STATUS_NEW, _now(), STATUS_SENDING, cutoff),
            )
            released = cur.rowcount
        if released:
            logger.warning(f"Возвращено в очередь зависших: {released}")
        return released

    def is_opted_out(self, seller_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM sellers WHERE seller_id = ?", (seller_id,)
            ).fetchone()
        return bool(row) and row["status"] == STATUS_OPTED_OUT

    def sent_since(self, since: datetime, account_id: Optional[str] = None) -> int:
        """Сколько сообщений реально ушло с указанного момента.

        Считаем по messages, а не по sellers: в sellers хранится только
        последнее сообщение и статус, который меняется вручную. И по sent_at,
        а не по updated_at — тот обновляется от любой правки, и отметка
        «ответил» съедала дневной лимит.
        """
        query = "SELECT COUNT(*) AS n FROM messages WHERE result = 'sent' AND sent_at >= ?"
        params: List[Any] = [since.isoformat(timespec="seconds")]
        if account_id is not None:
            query += " AND sender_account_id = ?"
            params.append(account_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def sent_today(self, account_id: Optional[str] = None) -> int:
        """Отправлено за сегодня по ЛОКАЛЬНОМУ времени.

        Граница суток берётся местная, а не UTC: для Москвы UTC-сутки
        сдвинуты на 3 часа, и лимит «за сутки» считался бы неверно.
        """
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc)
        return self.sent_since(start_utc, account_id=account_id)

    def sent_last_24h(self, account_id: Optional[str] = None) -> int:
        return self.sent_since(datetime.now(timezone.utc) - timedelta(hours=24), account_id=account_id)

    def template_usage(self) -> Dict[str, int]:
        """Сколько раз использовался каждый шаблон."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT template_name, COUNT(*) AS n FROM messages "
                "WHERE result = 'sent' AND template_name IS NOT NULL AND template_name != '' "
                "GROUP BY template_name"
            ).fetchall()
        return {r["template_name"]: int(r["n"]) for r in rows}

    def last_template_used(self) -> str:
        """Последний использованный шаблон для избежания повторов подряд."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT template_name FROM messages "
                "WHERE result = 'sent' AND template_name IS NOT NULL AND template_name != '' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row["template_name"]) if row and row["template_name"] else ""

    def template_performance(self) -> Dict[str, dict]:
        """A/B отдача каждого шаблона: отправки и положительные/нейтральные ответы."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m1.template_name, "
                "       COUNT(DISTINCT m1.seller_id) AS sent, "
                "       COUNT(DISTINCT CASE WHEN m2.result = 'received' AND m2.category != 'negative' THEN m2.seller_id END) AS replies "
                "FROM messages m1 "
                "LEFT JOIN messages m2 ON m1.seller_id = m2.seller_id "
                "WHERE m1.result = 'sent' AND m1.template_name IS NOT NULL AND m1.template_name != '' "
                "GROUP BY m1.template_name"
            ).fetchall()
        return {r["template_name"]: {"sent": int(r["sent"]), "replies": int(r["replies"])} for r in rows}

    def followup_candidates(
        self,
        after_days: int = 14,
        limit: int = 10,
        sender_account_id: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        """Кандидаты на вежливое повторное касание (фоллоу-ап).

        Отбираются продавцы со статусом 'sent', которым написали более after_days
        назад, без полученных ответов и без ранее отправленного фоллоу-апа.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).isoformat(
            timespec="seconds"
        )
        conditions = [
            "s.status = 'sent'",
            "s.sent_at != ''",
            "s.sent_at <= ?",
            "NOT EXISTS (SELECT 1 FROM messages WHERE seller_id = s.seller_id AND result = 'received')",
            "NOT EXISTS (SELECT 1 FROM messages WHERE seller_id = s.seller_id AND campaign_id = 'followup')",
        ]
        params: List[Any] = [cutoff]

        if sender_account_id == "main":
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM messages WHERE seller_id = s.seller_id "
                "AND sender_account_id IS NOT NULL AND sender_account_id != '' AND sender_account_id != 'main')"
            )
        elif sender_account_id:
            conditions.append(
                "EXISTS (SELECT 1 FROM messages WHERE seller_id = s.seller_id "
                "AND sender_account_id = ?)"
            )
            params.append(sender_account_id)

        where_clause = " AND ".join(conditions)
        query = (
            f"SELECT {SELECT_FIELDS} FROM sellers s "
            f"WHERE {where_clause} "
            f"ORDER BY s.sent_at ASC LIMIT ?"
        )
        params.append(limit)

        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def inbox_stats(self, hours: int = 24) -> dict:
        """Статистика входящих за последние N часов: всего ответов и горячих."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            replies = int(conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE result = 'received' AND sent_at >= ?",
                (cutoff,),
            ).fetchone()["n"])
            hot = int(conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE result = 'received' AND sent_at >= ? "
                "AND category IN ('positive', 'interested', 'needs_info')",
                (cutoff,),
            ).fetchone()["n"])
        return {"replies": replies, "hot": hot}

    def requeue_transient_chat_failures(self, older_than_minutes: int = 60) -> int:
        """Возвращает в очередь продавцов, у которых не открылся чат, спустя время."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? "
                "WHERE status = ? AND last_attempt_at < ? "
                "AND last_error LIKE '%чат не открылся%'",
                (STATUS_NEW, _now(), STATUS_FAILED, cutoff),
            )
            count = cur.rowcount
        if count:
            logger.info(f"Возвращено в очередь транзиентных сбоев чата: {count}")
        return count

    def queue_health(self) -> dict:
        """Сколько готово к отправке и сколько требует внимания."""
        with self._connect() as conn:
            ready = int(conn.execute(
                "SELECT COUNT(*) c FROM sellers WHERE status = ? "
                "AND verified_by IN (?, ?) AND sample_product_url != ''",
                (STATUS_NEW, VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["c"])
            failed = int(conn.execute(
                "SELECT COUNT(*) c FROM sellers WHERE status = ?", (STATUS_FAILED,)
            ).fetchone()["c"])
            no_product = int(conn.execute(
                "SELECT COUNT(*) c FROM sellers WHERE status = ? "
                "AND verified_by IN (?, ?) AND sample_product_url = ''",
                (STATUS_NEW, VERIFY_USCC, VERIFY_WAREHOUSE),
            ).fetchone()["c"])
        return {"ready": ready, "failed": failed, "no_product": no_product}

    def iter_sellers(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        with self._connect() as conn:
            if status:
                return conn.execute(
                    f"SELECT {SELECT_FIELDS} FROM sellers WHERE status = ? ORDER BY score DESC",
                    (status,),
                ).fetchall()
            return conn.execute(
                f"SELECT {SELECT_FIELDS} FROM sellers ORDER BY score DESC"
            ).fetchall()

    def stats(self) -> QueueStats:
        with self._connect() as conn:
            statuses = conn.execute(
                "SELECT status, COUNT(*) AS n FROM sellers GROUP BY status"
            ).fetchall()
            verified = conn.execute(
                "SELECT verified_by, COUNT(*) AS n FROM sellers GROUP BY verified_by"
            ).fetchall()
        by_status = {r["status"]: r["n"] for r in statuses}
        by_verification = {r["verified_by"]: r["n"] for r in verified}
        return QueueStats(
            total=sum(by_status.values()),
            by_status=by_status,
            by_verification=by_verification,
            sent_last_24h=self.sent_last_24h(),
        )

    def export_rows(self, verified_only: bool = True) -> List[sqlite3.Row]:
        """Все строки для выгрузки в CSV/Excel (без пагинации)."""
        return self.list_sellers(verified_only=verified_only, limit=1_000_000, offset=0)

    # --- Phase 2: CRM & Analytics ---

    def create_experiment(self, name: str, criteria: dict, variants: list) -> int:
        import json
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO experiments (name, criteria, variants, created_at) VALUES (?, ?, ?, ?)",
                (name, json.dumps(criteria), json.dumps(variants), _now())
            )
            return cursor.lastrowid

    def finish_experiment(self, exp_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE experiments SET status = 'finished', finished_at = ? WHERE id = ?", (_now(), exp_id))
            return cursor.rowcount > 0

    def assign_experiment_sellers(self, exp_id: int, sellers: list, variants: list) -> int:
        import random
        assigned = 0
        with self._connect() as conn:
            for s in sellers:
                v = random.choice(variants)
                try:
                    conn.execute("INSERT INTO experiment_sellers (experiment_id, seller_id, variant, assigned_at) VALUES (?, ?, ?, ?)",
                                 (exp_id, s, v, _now()))
                    assigned += 1
                except Exception:
                    pass
        return assigned

    def experiment_report(self, exp_id: int) -> dict:
        import json
        with self._connect() as conn:
            exp = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
            if not exp:
                return {"error": "not found"}
            variants = json.loads(exp["variants"])
            stats = {v: {"assigned": 0, "sent": 0, "replied": 0, "deals": 0} for v in variants}
            rows = conn.execute("SELECT variant, seller_id FROM experiment_sellers WHERE experiment_id = ?", (exp_id,)).fetchall()
            for r in rows:
                v = r["variant"]
                if v in stats:
                    stats[v]["assigned"] += 1
                    s = conn.execute("SELECT message_sent, stage FROM sellers WHERE seller_id = ?", (r["seller_id"],)).fetchone()
                    if s:
                        if s["message_sent"]: stats[v]["sent"] += 1
                        if s["stage"] in ('replied', 'interested', 'negotiation', 'deal'): stats[v]["replied"] += 1
                        if s["stage"] == 'deal': stats[v]["deals"] += 1
            return {"experiment": dict(exp), "variants": stats}

    def set_stage(self, seller_id: str, stage: str, reason: str = "", actor: str = "") -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE sellers SET stage = ?, updated_at = ? WHERE seller_id = ?", (stage, _now(), seller_id))
            if cursor.rowcount > 0:
                conn.execute("INSERT INTO funnel_transitions (seller_id, to_stage, reason, actor, event_at) VALUES (?, ?, ?, ?, ?)",
                             (seller_id, stage, reason, actor, _now()))
            return cursor.rowcount > 0

    def leads_distribution(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sellers").fetchone()[0]
            qualified = conn.execute("SELECT COUNT(*) FROM sellers WHERE verified_by IN ('uscc', 'warehouse')").fetchone()[0]
            grades = {}
            for row in conn.execute("SELECT lead_grade, COUNT(*) as c FROM sellers GROUP BY lead_grade").fetchall():
                grades[row["lead_grade"] or "U"] = row["c"]
            return {"total": total, "qualified": qualified, "grades": grades, "provinces": {}}

    def communication_stats(self) -> dict:
        with self._connect() as conn:
            sent = conn.execute("SELECT COUNT(*) FROM sellers WHERE message_sent != ''").fetchone()[0]
            replied = conn.execute("SELECT COUNT(*) FROM sellers WHERE stage IN ('replied', 'interested', 'negotiation', 'deal')").fetchone()[0]
            return {"sent": sent, "replied": replied}

    def funnel_counts(self) -> dict:
        with self._connect() as conn:
            counts = {}
            for row in conn.execute("SELECT stage, COUNT(*) as c FROM sellers GROUP BY stage").fetchall():
                counts[row["stage"] or "new"] = row["c"]
            return counts

    def funnel_transitions(self, limit: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.event_at, t.seller_id, s.shop_name, t.to_stage, t.reason, t.actor "
                "FROM funnel_transitions t LEFT JOIN sellers s ON t.seller_id = s.seller_id "
                "ORDER BY t.id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def recompute_all_lead_scores(self, config, limit: int = 0) -> int:
        from src.crm.leads import score_lead
        count = 0
        with self._connect() as conn:
            query = "SELECT * FROM sellers WHERE verified_by IN ('uscc', 'warehouse')"
            if limit > 0: query += f" LIMIT {limit}"
            rows = conn.execute(query).fetchall()
            for r in rows:
                d = dict(r)
                res = score_lead(d, [], config)
                conn.execute(
                    "UPDATE sellers SET commercial_score=?, engagement_score=?, priority=?, lead_grade=?, grade_reason=? WHERE seller_id=?",
                    (res["commercial"], res["engagement"], res["priority"], res["grade"], res["grade_reason"], r["seller_id"])
                )
                count += 1
        return count
