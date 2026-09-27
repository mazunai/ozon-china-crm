#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Резервные копии базы продавцов.

Зачем: база теперь единственное место, где живут продавцы, заметки и статусы.
Потеря файла означает потерю всей работы, поэтому копии делаются:

1. **локально** — `data/backups/` с ротацией (по умолчанию храним 20 последних);
2. **офсайт** — выгрузка в CSV, которую можно положить в приватный git-репозиторий
   (`--push`). Это копия вне машины, и для неё не нужно ничего настраивать,
   потому что репозиторий уже есть.

Копия снимается через `sqlite3.Connection.backup()`, а не копированием файла:
обычное копирование может поймать базу в середине транзакции и дать битый снимок.

Запуск:
    python tools/backup_db.py                 # снимок в data/backups/
    python tools/backup_db.py --list          # что уже есть
    python tools/backup_db.py --restore <файл>  # восстановить из снимка
    python tools/backup_db.py --push          # снимок + выгрузка + коммит в git
"""

import argparse
import csv
import logging
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import Settings

logger = logging.getLogger(__name__)

DB_PATH = Settings.BASE_DIR / "data" / "sellers.sqlite3"
BACKUP_DIR = Settings.BASE_DIR / "data" / "backups"
# Выгрузка лежит ОТДЕЛЬНО от data/: git не умеет брать файл из исключённого
# каталога, а нам нужно, чтобы CSV попадал в репозиторий как офсайт-копия.
EXPORT_DIR = Settings.BASE_DIR / "backups"
EXPORT_PATH = EXPORT_DIR / "sellers.csv"
KEEP_BACKUPS = 20

# Колонки выгрузки: они же попадут в git как читаемая офсайт-копия
EXPORT_COLUMNS = [
    "seller_id", "shop_name", "company_name", "uscc", "province",
    "registration_kind", "address", "orders", "reviews", "months_on_ozon",
    "rating", "score", "verified_by", "status", "notes",
    "seller_profile_url", "sample_product_url", "chinese_reasons",
    "first_seen_at", "last_seen_at",
]


def snapshot(db_path: Path = DB_PATH) -> Path:
    """Снимает согласованный снимок базы.

    Если база не существует — это не ошибка: значит, сбор ещё не запускали.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"База не найдена: {db_path}\n"
            "Она появится после первого запуска: python run_harvest.py harvest"
        )

    # Диск, забитый под завязку, даёт частичный снимок — а мы его потом
    # «восстановим». Лучше отказаться заранее (аудит)
    import shutil as _shutil

    free = _shutil.disk_usage(BACKUP_DIR).free
    need = db_path.stat().st_size * 2
    if free < max(need, 50 * 1024 * 1024):
        raise RuntimeError(
            f"На диске свободно {free // (1024 * 1024)} МБ — меньше двойного "
            f"размера базы. Освободите место, иначе снимок уйдёт битым")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"sellers-{stamp}.sqlite3"

    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(target)
        try:
            # Ключевой момент: backup() читает базу согласованно, даже если
            # прямо сейчас идёт запись. Копирование файла так не умеет.
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    # Проверяем, что снимок читается и содержит нашу таблицу
    check = sqlite3.connect(target)
    try:
        count = check.execute("SELECT COUNT(*) FROM sellers").fetchone()[0]
    finally:
        check.close()

    logger.info(f"Снимок готов: {target.name} ({count} продавцов)")
    return target


def list_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("sellers-*.sqlite3"))


def rotate(keep: int = KEEP_BACKUPS) -> int:
    """Удаляет старые снимки, оставляя последние `keep`."""
    backups = list_backups()
    if len(backups) <= keep:
        return 0
    removed = 0
    for path in backups[:-keep]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:                                # noqa: BLE001
            logger.warning(f"Не удалось удалить {path.name}: {exc}")
    if removed:
        logger.info(f"Ротация: удалено старых снимков {removed}, оставлено {keep}")
    return removed


def export_csv(db_path: Path = DB_PATH, verified_only: bool = True) -> Path:
    """Выгружает продавцов в CSV — читаемая копия для git.

    По умолчанию только ПОДТВЕРЖДЁННЫЕ (USCC или склад в Китае). Неподтверждённые
    в выгрузку не идут: это список для работы, и российской компании в нём быть
    не должно. Раньше фильтра не было, и в CSV попадали все подряд — включая
    продавцов, которых перепроверка понизила.

    Полный список со всеми уровнями — `--all`.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"База не найдена: {db_path}")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = EXPORT_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "WHERE verified_by IN ('uscc', 'warehouse')" if verified_only else ""
        rows = conn.execute(
            f"SELECT {', '.join(EXPORT_COLUMNS)} FROM sellers {where} ORDER BY score DESC"
        ).fetchall()
    finally:
        conn.close()

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            writer.writerow([row[column] for column in EXPORT_COLUMNS])

    logger.info(f"Выгрузка готова: {target} ({len(rows)} продавцов)")
    return target


def export_messages(db_path: Path = DB_PATH) -> Path:
    """Выгружает журнал переписки в CSV — второй офсайт-файл рядом с sellers.csv.

    Аудит: messages в офсайт не попадали никогда, при этом полный текст
    ответов — ключевое требование проекта «ни один ответ не теряется».
    """
    if not db_path.exists():
        raise FileNotFoundError(f"База не найдена: {db_path}")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = EXPORT_DIR / "messages.csv"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT m.id, m.seller_id, s.shop_name, m.text, m.sent_at, "
            "m.result, m.category, m.error "
            "FROM messages m LEFT JOIN sellers s ON s.seller_id = m.seller_id "
            "ORDER BY m.id"
        ).fetchall()
    finally:
        conn.close()

    columns = ["id", "seller_id", "shop_name", "text", "sent_at", "result", "category", "error"]
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])

    logger.info(f"Выгрузка журнала переписки: {target} ({len(rows)} строк)")
    return target


def push_to_git(message: str = "") -> bool:
    """Коммитит выгрузку в приватный репозиторий — это и есть офсайт-копия.

    Сам файл базы в git не кладём: он бинарный и меняется целиком при каждом
    сохранении. А CSV читаемый, диффится и хорошо переживает историю.
    """
    repo = Settings.BASE_DIR
    try:
        subprocess.run(["git", "-C", str(repo), "add", "backups/sellers.csv",
                        "backups/messages.csv"],
                       check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m",
             message or f"Бэкап базы продавцов {datetime.now():%Y-%m-%d %H:%M}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Ничего не изменилось — это нормально, не ошибка
            logger.info("Коммитить нечего: выгрузка не изменилась")
            return False

        push = subprocess.run(["git", "-C", str(repo), "push", "-q", "crm", "HEAD"],
                              capture_output=True, text=True)
        if push.returncode != 0:
            logger.warning(f"Коммит создан, но push не прошёл: {push.stderr.strip()[:200]}")
            return False
        logger.info("Офсайт-копия отправлена в репозиторий")
        return True
    except subprocess.CalledProcessError as exc:              # noqa: BLE001
        logger.warning(f"Git недоступен: {exc}")
        return False


def restore(backup_path: Path, db_path: Path = DB_PATH) -> Path:
    """Восстанавливает базу из снимка, сохранив текущую как .before-restore."""
    if not backup_path.exists():
        raise FileNotFoundError(f"Снимок не найден: {backup_path}")

    if db_path.exists():
        # Живой дашборд держит соединения и продолжит писать в старый файл:
        # восстановление поверх него бессмысленно и опасно (аудит)
        pid_file = db_path.parent / "local_server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8").strip() or 0), 0)
                raise RuntimeError(
                    "Локальный дашборд запущен (data/local_server.pid) — "
                    "остановите его: tools/local_server.sh stop")
            except (ProcessLookupError, ValueError):
                pass
        # Safety-копия — согласованный снимок через backup(), а не копия
        # файла: в WAL-режиме копия основного файла пуста или битая
        safety = db_path.with_suffix(".sqlite3.before-restore")
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        target = sqlite3.connect(safety)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        logger.info(f"Текущая база сохранена как {safety.name}")
        # Убираем WAL-хвосты, иначе они «перекроют» восстановленный файл
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    shutil.copy2(backup_path, db_path)
    logger.info(f"База восстановлена из {backup_path.name}")
    return db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Резервные копии базы продавцов")
    parser.add_argument("--list", action="store_true", help="показать имеющиеся снимки")
    parser.add_argument("--restore", metavar="ФАЙЛ", help="восстановить базу из снимка")
    parser.add_argument("--keep", type=int, default=KEEP_BACKUPS, help="сколько снимков хранить")
    parser.add_argument("--push", action="store_true",
                        help="дополнительно выгрузить в CSV и отправить в git")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="не делать снимок (только выгрузка)")
    parser.add_argument("--all", action="store_true",
                        help="в выгрузку включить и неподтверждённых (по умолчанию только подтверждённые)")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()

    if args.list:
        backups = list_backups()
        if not backups:
            print(f"Снимков нет. Каталог: {BACKUP_DIR}")
            return 0
        print(f"Снимки в {BACKUP_DIR}:")
        for path in backups:
            size = path.stat().st_size / 1024
            print(f"  {path.name}  {size:>8.1f} КБ")
        return 0

    if args.restore:
        try:
            restore(Path(args.restore))
        except FileNotFoundError as exc:
            print(exc)
            return 1
        print("Готово. Перезапустите дашборд, чтобы он открыл восстановленную базу.")
        return 0

    try:
        if not args.no_snapshot:
            target = snapshot()
            print(f"Снимок: {target}")
            rotate(args.keep)

        if args.push:
            export = export_csv(verified_only=not args.all)
            print(f"Выгрузка: {export}")
            messages = export_messages()
            print(f"Выгрузка журнала переписки: {messages}")
            push_to_git()
    except FileNotFoundError as exc:
        print(exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
