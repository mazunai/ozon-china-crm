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
    # Когда сообщение РЕАЛЬНО ушло. Нужна отдельно от updated_at: тот меняется
    # от любой правки (заметка, смена статуса), и дневной лимит считался по
    # нему — отметка «ответил» съедала лимит и сокращала отправки.
    "sent_at": "TEXT NOT NULL DEFAULT ''",
}

# Журнал отправок. В sellers.message_sent хранится только последнее сообщение
# и оно перезаписывается, поэтому «что писали и что было на прошлой попытке»
# узнать было нельзя.
MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id   TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT 'sent',
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_seller ON messages(seller_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at DESC);
"""

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
    "sent_at"
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

    @staticmethod
    def _build_filter(
        verified_only: bool = True,
        statuses: Optional[Sequence[str]] = None,
        provinces: Optional[Sequence[str]] = None,
        search: str = "",
        tiers: Optional[Sequence[str]] = None,
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
    ) -> List[sqlite3.Row]:
        """Список продавцов для дашборда.

        По умолчанию отдаёт только подтверждённых (USCC или склад в Китае) —
        эвристики дают ложные плюсы, и показывать их вперемешку нельзя.
        """
        allowed_order = {
            "score", "orders", "reviews", "months_on_ozon", "rating",
            "shop_name", "company_name", "province", "status", "last_seen_at",
        }
        order_by = order_by if order_by in allowed_order else "score"

        where, params = self._build_filter(
            verified_only=verified_only and not include_unverified,
            statuses=statuses,
            provinces=provinces,
            search=search,
            tiers=tiers,
        )
        sql = (
            f"SELECT {SELECT_FIELDS} FROM sellers{where} "
            f"ORDER BY {order_by} {'DESC' if descending else 'ASC'} LIMIT ? OFFSET ?"
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
    ) -> int:
        """Число записей под теми же условиями, что и list_sellers."""
        where, params = self._build_filter(
            verified_only=verified_only,
            statuses=statuses,
            provinces=provinces,
            search=search,
            tiers=tiers,
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

        Нужно для чистки неподтверждённых: они скрыты в интерфейсе, но лежат в
        файле базы и попадают в выгрузку «--all».
        """
        if not seller_ids:
            return 0
        with self._connect() as conn:
            placeholders = ",".join("?" * len(seller_ids))
            cur = conn.execute(
                f"DELETE FROM sellers WHERE seller_id IN ({placeholders})",
                list(seller_ids),
            )
            deleted = cur.rowcount
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

    def claim_next(self, require_product_url: bool = False) -> Optional[sqlite3.Row]:
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
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sellers SET status = ?, message_sent = ?, last_error = '', "
                "sent_at = ?, updated_at = ? WHERE seller_id = ?",
                (STATUS_SENT, message, now, now, seller_id),
            )
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result) VALUES (?, ?, ?, 'sent')",
                (seller_id, message, now),
            )
        logger.info(f"Отправлено: {seller_id}")

    def log_message(self, seller_id: str, text: str, result: str, error: str = "") -> None:
        """Записывает попытку в журнал, не меняя статус продавца."""
        import json as _json
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (seller_id, text, sent_at, result, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (seller_id, text, _now(), result, _json.dumps(error[:300], ensure_ascii=False)[1:-1]),
            )

    def message_history(self, seller_id: str, limit: int = 50) -> List[sqlite3.Row]:
        """История переписки с продавцом, свежие сверху."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT text, sent_at, result, error FROM messages "
                "WHERE seller_id = ? ORDER BY sent_at DESC, id DESC LIMIT ?",
                (seller_id, limit),
            ).fetchall()

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
        """Сколько сообщений реально ушло с указанного момента.

        Считаем по messages, а не по sellers: в sellers хранится только
        последнее сообщение и статус, который меняется вручную. И по sent_at,
        а не по updated_at — тот обновляется от любой правки, и отметка
        «ответил» съедала дневной лимит.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE result = 'sent' AND sent_at >= ?",
                (since.isoformat(timespec="seconds"),),
            ).fetchone()
        return int(row["n"]) if row else 0

    def sent_today(self) -> int:
        """Отправлено за сегодня по ЛОКАЛЬНОМУ времени.

        Граница суток берётся местная, а не UTC: для Москвы UTC-сутки
        сдвинуты на 3 часа, и лимит «за сутки» считался бы неверно.
        """
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc)
        return self.sent_since(start_utc)

    def sent_last_24h(self) -> int:
        return self.sent_since(datetime.now(timezone.utc) - timedelta(hours=24))

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
