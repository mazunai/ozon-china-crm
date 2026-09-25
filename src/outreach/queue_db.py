"""
Очередь продавцов для рассылки на SQLite.

Почему отдельная БД, а не JSON из output/: нужны три вещи, которых в файле
результатов нет.

1. Дедупликация по seller_id — одному продавцу пишем один раз, даже если он
   встретился в двадцати товарах и в трёх запусках парсера.
2. Атомарная выдача «следующий на отправку» — чтобы падение процесса в момент
   отправки не привело к повторному сообщению.
3. Стоп-лист — продавцы, которые попросили больше не писать, исключаются
   навсегда и не зависят от содержимого output/.
"""

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from .scoring import SellerMetrics

logger = logging.getLogger(__name__)

# Статусы продавца в очереди
STATUS_NEW = "new"                # ждёт отправки
STATUS_SENDING = "sending"        # взяли в работу прямо сейчас
STATUS_SENT = "sent"              # сообщение отправлено
STATUS_FAILED = "failed"          # попытка не удалась, можно повторить
STATUS_SKIPPED = "skipped"        # исключён вручную
STATUS_OPTED_OUT = "opted_out"    # попросил не писать — больше не трогаем

TERMINAL_STATUSES = (STATUS_SENT, STATUS_SKIPPED, STATUS_OPTED_OUT)

SCHEMA = """
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
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sellers_status_score
    ON sellers(status, score DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class QueueStats:
    total: int = 0
    by_status: dict = None
    sent_last_24h: int = 0

    def __post_init__(self):
        if self.by_status is None:
            self.by_status = {}

    def as_text(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.by_status.items())]
        return f"всего={self.total} ({', '.join(parts) or 'пусто'}), за сутки отправлено={self.sent_last_24h}"


class OutreachQueue:
    """Очередь на SQLite. Один файл, один процесс-отправитель."""

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
            conn.executescript(SCHEMA)

    # --- Наполнение ---------------------------------------------------------

    def add_sellers(self, sellers: Sequence[SellerMetrics]) -> int:
        """Добавляет продавцов, обновляя метрики у уже известных.

        Статус и историю попыток НЕ перезаписывает: если продавцу уже писали,
        повторный импорт из output/ не вернёт его в очередь на отправку.
        """
        if not sellers:
            return 0

        added = 0
        with self._connect() as conn:
            for s in sellers:
                existing = conn.execute(
                    "SELECT seller_id FROM sellers WHERE seller_id = ?", (s.seller_id,)
                ).fetchone()

                if existing:
                    conn.execute(
                        """UPDATE sellers SET
                               shop_name = ?, company_name = ?, uscc = ?, province = ?,
                               orders = ?, reviews = ?, months_on_ozon = ?, rating = ?,
                               score = ?, is_chinese = ?,
                               sample_product_url = CASE
                                   WHEN sample_product_url = '' THEN ? ELSE sample_product_url END,
                               updated_at = ?
                           WHERE seller_id = ?""",
                        (
                            s.shop_name, s.company_name, s.uscc, s.province,
                            s.orders, s.reviews, s.months_on_ozon, s.rating,
                            s.score, int(s.is_chinese),
                            s.sample_product_url, _now(), s.seller_id,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sellers (
                               seller_id, shop_name, company_name, uscc, province,
                               orders, reviews, months_on_ozon, rating, score,
                               is_chinese, sample_product_url, status,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            s.seller_id, s.shop_name, s.company_name, s.uscc, s.province,
                            s.orders, s.reviews, s.months_on_ozon, s.rating, s.score,
                            int(s.is_chinese), s.sample_product_url, STATUS_NEW,
                            _now(), _now(),
                        ),
                    )
                    added += 1
        logger.info(f"Импорт в очередь: добавлено {added}, обновлено {len(sellers) - added}")
        return added

    # --- Выдача работы -----------------------------------------------------

    def claim_next(self, require_product_url: bool = True) -> Optional[sqlite3.Row]:
        """Возвращает следующего продавца и помечает его как «в работе».

        Сортировка по score DESC: первыми идут самые крупные и «долгоживущие»
        продавцы. Требуем непустой sample_product_url — без него некуда открыть
        чат, и продавец застрянет в очереди навсегда.
        """
        with self._connect() as conn:
            where = "status = ?"
            params: List = [STATUS_NEW]
            if require_product_url:
                where += " AND sample_product_url != ''"

            row = conn.execute(
                f"SELECT * FROM sellers WHERE {where} ORDER BY score DESC LIMIT 1",
                params,
            ).fetchone()
            if not row:
                return None

            conn.execute(
                "UPDATE sellers SET status = ?, attempts = attempts + 1, "
                "last_attempt_at = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_SENDING, _now(), _now(), row["seller_id"]),
            )
            logger.info(f"В работу: {row['shop_name'] or row['seller_id']} (score={row['score']:.2f})")
            return row

    def mark_sent(self, seller_id: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, message_sent = ?, last_error = '', "
                "updated_at = ? WHERE seller_id = ?",
                (STATUS_SENT, message, _now(), seller_id),
            )
        logger.info(f"Отправлено: {seller_id}")

    def mark_failed(self, seller_id: str, error: str, retryable: bool = True) -> None:
        """Помечает неудачу. retryable=True — вернёт в очередь на повтор."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, last_error = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_FAILED if retryable else STATUS_SKIPPED,
                 error[:500], _now(), seller_id),
            )
        logger.warning(f"Не отправлено: {seller_id} — {error[:200]}")

    def mark_opted_out(self, seller_id: str) -> None:
        """Продавец попросил не писать — исключаем навсегда."""
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
        """Возвращает в очередь зависшие «в работе» записи.

        Нужно после аварийного завершения: продавец помечен sending, но
        сообщение не ушло. Без этого он застрянет навсегда.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sellers SET status = ?, updated_at = ? "
                "WHERE status = ? AND last_attempt_at < ?",
                (STATUS_NEW, _now(), STATUS_SENDING, cutoff),
            )
            released = cur.rowcount
        if released:
            logger.warning(f"Возвращено в очередь зависших записей: {released}")
        return released

    def is_opted_out(self, seller_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM sellers WHERE seller_id = ?", (seller_id,)
            ).fetchone()
        return bool(row) and row["status"] == STATUS_OPTED_OUT

    # --- Отчётность --------------------------------------------------------

    def sent_since(self, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sellers WHERE status = ? AND updated_at >= ?",
                (STATUS_SENT, since.isoformat(timespec="seconds")),
            ).fetchone()
        return int(row["n"]) if row else 0

    def sent_last_24h(self) -> int:
        return self.sent_since(datetime.now(timezone.utc) - timedelta(hours=24))

    def stats(self) -> QueueStats:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM sellers GROUP BY status"
            ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        return QueueStats(
            total=sum(by_status.values()),
            by_status=by_status,
            sent_last_24h=self.sent_last_24h(),
        )

    def iter_sellers(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        with self._connect() as conn:
            if status:
                return conn.execute(
                    "SELECT * FROM sellers WHERE status = ? ORDER BY score DESC", (status,)
                ).fetchall()
            return conn.execute("SELECT * FROM sellers ORDER BY score DESC").fetchall()
