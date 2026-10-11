#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Утилита слияния двух баз sellers.sqlite3 (например, базы с ВМ и локальной базы с Мака).

Правила объединения:
1. Новые продавцы, которых нет в целевой базе, добавляются.
2. Для существующих продавцов статус берётся по наивысшему приоритету:
   replied > sent > opted_out > skipped > failed > new.
   (То есть если продавцу уже писали на любой из машин, он гарантированно останется 'sent'/'replied').
3. Заметки менеджера (manager_note) объединяются без потери текста.
4. История отправленных сообщений (messages) переносится без дубликатов.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

STATUS_PRIORITY = {
    "replied": 6,
    "sent": 5,
    "opted_out": 4,
    "skipped": 3,
    "failed": 2,
    "new": 1,
}

def merge_databases(source_path: Path, target_path: Path) -> dict:
    if not source_path.exists():
        raise FileNotFoundError(f"Исходная база не найдена: {source_path}")
    if not target_path.exists():
        raise FileNotFoundError(f"Целевая база не найдена: {target_path}")

    s_conn = sqlite3.connect(source_path)
    s_conn.row_factory = sqlite3.Row
    t_conn = sqlite3.connect(target_path)
    t_conn.row_factory = sqlite3.Row

    s_cur = s_conn.cursor()
    t_cur = t_conn.cursor()

    # 1. Читаем всех продавцов из source
    s_cur.execute("SELECT * FROM sellers")
    source_sellers = {r["seller_id"]: dict(r) for r in s_cur.fetchall()}

    # 2. Читаем всех продавцов из target
    t_cur.execute("SELECT * FROM sellers")
    target_sellers = {r["seller_id"]: dict(r) for r in t_cur.fetchall()}

    stats = {
        "inserted": 0,
        "status_upgraded": 0,
        "notes_merged": 0,
        "messages_copied": 0,
    }

    t_cols = [r[1] for r in t_cur.execute("PRAGMA table_info(sellers)").fetchall()]

    for s_id, s_row in source_sellers.items():
        if s_id not in target_sellers:
            # Вставляем нового продавца
            cols = [c for c in t_cols if c in s_row]
            placeholders = ", ".join(["?"] * len(cols))
            vals = [s_row[c] for c in cols]
            sql = f"INSERT INTO sellers ({', '.join(cols)}) VALUES ({placeholders})"
            t_cur.execute(sql, vals)
            stats["inserted"] += 1
        else:
            # Обновляем существующего продавца
            t_row = target_sellers[s_id]
            t_prio = STATUS_PRIORITY.get(t_row.get("status"), 0)
            s_prio = STATUS_PRIORITY.get(s_row.get("status"), 0)

            updates = {}
            if s_prio > t_prio:
                updates["status"] = s_row["status"]
                updates["last_error"] = s_row.get("last_error", "")
                stats["status_upgraded"] += 1

            # Мержим заметки
            t_note = (t_row.get("manager_note") or "").strip()
            s_note = (s_row.get("manager_note") or "").strip()
            if s_note and s_note != t_note:
                if not t_note:
                    updates["manager_note"] = s_note
                    stats["notes_merged"] += 1
                elif s_note not in t_note:
                    updates["manager_note"] = f"{t_note} | {s_note}"
                    stats["notes_merged"] += 1

            if updates:
                set_clauses = [f"{k} = ?" for k in updates]
                vals = list(updates.values()) + [s_id]
                t_cur.execute(f"UPDATE sellers SET {', '.join(set_clauses)} WHERE seller_id = ?", vals)

    # 3. Переносим сообщения из messages
    try:
        s_cur.execute("SELECT * FROM messages")
        s_messages = s_cur.fetchall()
        t_msg_cols = [r[1] for r in t_cur.execute("PRAGMA table_info(messages)").fetchall()]

        for msg in s_messages:
            msg_dict = dict(msg)
            # Проверяем наличие по (seller_id, sent_at)
            t_cur.execute("SELECT id FROM messages WHERE seller_id = ? AND sent_at = ?",
                          (msg_dict["seller_id"], msg_dict["sent_at"]))
            if not t_cur.fetchone():
                cols = [c for c in t_msg_cols if c in msg_dict and c != "id"]
                placeholders = ", ".join(["?"] * len(cols))
                vals = [msg_dict[c] for c in cols]
                t_cur.execute(f"INSERT INTO messages ({', '.join(cols)}) VALUES ({placeholders})", vals)
                stats["messages_copied"] += 1
    except Exception as exc:
        print(f"Предупреждение при переносе сообщений: {exc}")

    t_conn.commit()
    s_conn.close()
    t_conn.close()
    return stats

def main():
    parser = argparse.ArgumentParser(description="Слияние двух баз sellers.sqlite3")
    parser.add_argument("source", type=Path, help="База-донор (откуда берём данные)")
    parser.add_argument("target", type=Path, help="Целевая база (куда вливаем)")
    args = parser.parse_args()

    stats = merge_databases(args.source, args.target)
    print("Слияние завершено успешно:")
    print(f"  • Добавлено новых продавцов: {stats['inserted']}")
    print(f"  • Обновлено статусов (на более высокий): {stats['status_upgraded']}")
    print(f"  • Объединено заметок менеджеров: {stats['notes_merged']}")
    print(f"  • Скопировано сообщений: {stats['messages_copied']}")

if __name__ == "__main__":
    main()
