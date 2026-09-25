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
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..outreach.scoring import SellerMetrics

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
}

SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS sellers (
    seller_id           TEXT PRIMARY KEY,
    shop_name           TEXT NOT NULL DEFAULT '',
    company_name        TEXT NOT NULL DEFAULT '',
    uscc                TEXT NOT NULL DEFAULT '',
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
    chinese_reasons     TEXT NOT NULL DEFAULT ''
);
"""

# Индексы создаются ПОСЛЕ миграции: на старой базе очереди колонки verified_by
# ещё нет, и CREATE INDEX по ней упал бы.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sellers_status_score ON sellers(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_sellers_verified ON sellers(verified_by, score DESC);
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
    "last_error, message_sent, first_seen_at, last_seen_at, product_count, chinese_reasons"
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
                    conn.execute(
                        """UPDATE sellers SET
                               shop_name = ?, company_name = ?, uscc = ?, province = ?,
                               orders = ?, reviews = ?, months_on_ozon = ?, rating = ?,
                               score = ?, is_chinese = ?,
                               verified_by = CASE WHEN ? != 'none' THEN ? ELSE verified_by END,
                               seller_profile_url = CASE WHEN seller_profile_url = '' THEN ? ELSE seller_profile_url END,
                               sample_product_url = CASE WHEN sample_product_url = '' THEN ? ELSE sample_product_url END,
                               chinese_reasons = CASE WHEN ? != '' THEN ? ELSE chinese_reasons END,
                               last_seen_at = ?, updated_at = ?
                           WHERE seller_id = ?""",
                        (
                            s.shop_name, s.company_name, s.uscc, s.province,
                            s.orders, s.reviews, s.months_on_ozon, s.rating,
                            s.score, int(s.is_chinese),
                            verified, verified,
                            getattr(s, "seller_profile_url", "") or "",
                            s.sample_product_url,
                            getattr(s, "chinese_reasons", "") or "",
                            getattr(s, "chinese_reasons", "") or "",
                            now, now, s.seller_id,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sellers (
                               seller_id, shop_name, company_name, uscc, province,
                               orders, reviews, months_on_ozon, rating, score,
                               is_chinese, verified_by, registration_kind,
                               seller_profile_url, sample_product_url, chinese_reasons,
                               status, first_seen_at, last_seen_at, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            s.seller_id, s.shop_name, s.company_name, s.uscc, s.province,
                            s.orders, s.reviews, s.months_on_ozon, s.rating, s.score,
                            int(s.is_chinese), verified,
                            getattr(s, "registration_kind", "unknown") or "unknown",
                            getattr(s, "seller_profile_url", "") or "",
                            s.sample_product_url,
                            getattr(s, "chinese_reasons", "") or "",
                            STATUS_NEW, now, now, now, now,
                        ),
                    )
                    added += 1
        logger.info(f"Импорт: добавлено {added}, обновлено {len(sellers) - added}")
        return added

    # --- Выборка для дашборда --------------------------------------------

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
    ) -> List[sqlite3.Row]:
        """Список продавцов для дашборда.

        По умолчанию отдаёт только подтверждённых (USCC или склад в Китае) —
        эвристики дают ложные плюсы, и показывать их вперемешку нельзя.
        """
        allowed_order = {
            "score", "orders", "reviews", "months_on_ozon", "rating",
            "shop_name", "status", "last_seen_at",
        }
        order_by = order_by if order_by in allowed_order else "score"

        where, params = [], []
        if verified_only and not include_unverified:
            where.append("verified_by IN (?, ?)")
            params.extend([VERIFY_USCC, VERIFY_WAREHOUSE])
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if search:
            where.append("(shop_name LIKE ? OR company_name LIKE ? OR uscc LIKE ? OR notes LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern, pattern])

        sql = f"SELECT {SELECT_FIELDS} FROM sellers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'} LIMIT ? OFFSET ?"
        params.extend([max(1, limit), max(0, offset)])

        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def count_sellers(self, verified_only: bool = True, search: str = "") -> int:
        where, params = [], []
        if verified_only:
            where.append("verified_by IN (?, ?)")
            params.extend([VERIFY_USCC, VERIFY_WAREHOUSE])
        if search:
            where.append("(shop_name LIKE ? OR company_name LIKE ? OR uscc LIKE ? OR notes LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern, pattern])
        sql = "SELECT COUNT(*) AS n FROM sellers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()["n"])

    def get_seller(self, seller_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                f"SELECT {SELECT_FIELDS} FROM sellers WHERE seller_id = ?", (seller_id,)
            ).fetchone()

    # --- Правки из дашборда ----------------------------------------------

    def set_note(self, seller_id: str, note: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET notes = ?, updated_at = ? WHERE seller_id = ?",
                (note, _now(), seller_id),
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

    def claim_next(self, require_product_url: bool = True) -> Optional[sqlite3.Row]:
        """Следующий продавец на отправку и пометка «в работе».

        Сортировка по score DESC: сначала самые крупные и долгоживущие.
        Отправляем только подтверждённых — эвристики не берём, чтобы не писать
        российским продавцам.
        """
        with self._connect() as conn:
            where = "status = ? AND verified_by IN (?, ?)"
            params: List = [STATUS_NEW, VERIFY_USCC, VERIFY_WAREHOUSE]
            if require_product_url:
                where += " AND sample_product_url != ''"

            row = conn.execute(
                f"SELECT * FROM sellers WHERE {where} ORDER BY score DESC LIMIT 1", params
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

    def mark_sent(self, seller_id: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, message_sent = ?, last_error = '', "
                "updated_at = ? WHERE seller_id = ?",
                (STATUS_SENT, message, _now(), seller_id),
            )
        logger.info(f"Отправлено: {seller_id}")

    def mark_failed(self, seller_id: str, error: str, retryable: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, last_error = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_FAILED if retryable else STATUS_SKIPPED, error[:500], _now(), seller_id),
            )
        logger.warning(f"Не отправлено: {seller_id} — {error[:200]}")

    def mark_opted_out(self, seller_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_OPTED_OUT, _now(), seller_id),
            )
        logger.info(f"В стоп-лист: {seller_id}")

    def mark_skipped(self, seller_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_SKIPPED, _now(), seller_id),
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

    def sent_since(self, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sellers WHERE status IN (?, ?) AND updated_at >= ?",
                (STATUS_SENT, STATUS_REPLIED, since.isoformat(timespec="seconds")),
            ).fetchone()
        return int(row["n"]) if row else 0

    def sent_last_24h(self) -> int:
        return self.sent_since(datetime.now(timezone.utc) - timedelta(hours=24))

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
