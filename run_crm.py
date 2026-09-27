#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёты по коммуникации с продавцами — точка входа.

    python run_crm.py report                      # сводка: кому писали, кто ответил
    python run_crm.py interested                  # заинтересованные (главный список)
    python run_crm.py no-reply                    # писали, но не ответили
    python run_crm.py replies                     # все ответы с текстом
    python run_crm.py replies --category negative # ответы одной категории
    python run_crm.py seller ABC-Store            # карточка: что отправили, что ответил
    python run_crm.py handover <seller_id>        # передать продавца менеджеру
    python run_crm.py recategorize --all          # пересчитать категории по правилам
    python run_crm.py backfill [seller]           # дотянуть полный текст ответа из чата

Данные берутся из общей базы `data/sellers.sqlite3` — той же, куда пишет
рассылка и разбор входящих. Отдельной системы нет: отчёты — только выборки.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import Settings
from src.crm.store import (
    INTERESTED_CATEGORIES,
    REPLY_CATEGORY_LABELS,
    STATUS_LABELS,
    SellerStore,
)

DB_PATH = Settings.BASE_DIR / "data" / "sellers.sqlite3"
# Селекторы чата — те же, что у отправщика: полный текст снимается через
# тот же открытый чат.
DATA_DIR = Settings.BASE_DIR / "outreach_data"


def _category_label(key: str) -> str:
    return REPLY_CATEGORY_LABELS.get(key, key or "—")


def _fmt_date(iso: str) -> str:
    """'2026-09-25T14:30:00+00:00' -> '25.09.2026 14:30' (UTC — как в базе)."""
    if not iso:
        return ""
    return iso[:16].replace("T", " ").replace("-", ".")


def _shop(row) -> str:
    # row бывает и dict (comm_report_rows), и sqlite3.Row (get_seller):
    # у Row нет метода .get() — обращаемся по ключу с проверкой наличия
    try:
        return row.get("shop_name") or row.get("seller_id") or "?"
    except AttributeError:
        keys = row.keys()
        name = row["shop_name"] if "shop_name" in keys else ""
        return name or row["seller_id"] or "?"


def _handover_state(row) -> str:
    """Статус работы для отчёта: в чьих руках продавец."""
    if row.get("handed_to_manager_at"):
        return "У МЕНЕДЖЕРА"
    if row.get("reply_category") in INTERESTED_CATEGORIES:
        return "НУЖНА РАБОТА МЕНЕДЖЕРА"
    return "В РАБОТЕ БОТА"


def print_comm_card(row: dict) -> None:
    """Карточка коммуникации — формат из требований проекта."""
    reply_text = (row.get("last_reply_text") or "").strip()
    sent_text = (row.get("last_sent_text") or "").strip()

    print(f"Продавец: {_shop(row)}")
    if row.get("company_name"):
        print(f"Юрлицо: {row['company_name']}")
    if row.get("last_sent_at") and sent_text:
        print(f"Сообщение отправлено: {_fmt_date(row['last_sent_at'])}")
        first = sent_text.splitlines()[0]
        print(f"Сообщение: \"{first[:120]}{'…' if len(sent_text) > 120 else ''}\"")
    else:
        print("Сообщение отправлено: нет")
    print(f"Ответ: \"{reply_text[:400]}{'…' if len(reply_text) > 400 else ''}\""
          if reply_text else "Ответ: нет")
    print(f"Категория: {_category_label(row.get('reply_category', ''))}")
    if row.get("reply_at"):
        print(f"Ответ получен: {_fmt_date(row['reply_at'])}")
    print(f"Статус рассылки: {STATUS_LABELS.get(row.get('status', ''), row.get('status', ''))}")
    print(f"Статус работы: {_handover_state(row)}")


def cmd_report(args) -> int:
    store = SellerStore(DB_PATH)
    comm = store.communication_counts()
    counts = store.category_counts()
    stages = store.stage_counts()

    print("СВОДКА ПО КОММУНИКАЦИЯМ")
    print(f"  Всего продавцов в базе:   {comm['total']}")
    print(f"  Кому отправили сообщение: {comm['messaged']}")
    print(f"  Не ответили:              {comm['no_reply']}")
    print(f"  Ответили:                 {comm['replied']}")
    print(f"  Передано менеджеру:       {comm['handed']}")
    interested = sum(counts.get(c, 0) for c in INTERESTED_CATEGORIES)
    print(f"  Заинтересованные:         {interested}")
    print()
    print("КАТЕГОРИИ ПОСЛЕДНИХ ОТВЕТОВ")
    if not counts:
        print("  ответов пока нет")
    for key in REPLY_CATEGORY_LABELS:
        if counts.get(key):
            print(f"  {REPLY_CATEGORY_LABELS[key]:<32} {counts[key]}")
    if stages:
        print()
        print("СТАДИИ СДЕЛКИ")
        for key, n in stages.items():
            print(f"  {SellerStore.STAGE_LABELS.get(key, key):<32} {n}")
    return 0


def cmd_interested(args) -> int:
    store = SellerStore(DB_PATH)
    rows = store.interested_rows(limit=args.limit)
    print("ЗАИНТЕРЕСОВАННЫЕ")
    if not rows:
        print("  пока никого — либо ответов нет, либо все негативные")
        return 0
    for index, row in enumerate(rows, 1):
        reply_text = (row.get("last_reply_text") or "").strip()
        print(f"\n{index}. {_shop(row)}"
              + (f" ({row['province']})" if row.get("province") else ""))
        print(f"   Ответ: \"{reply_text[:200]}{'…' if len(reply_text) > 200 else ''}\"")
        print(f"   Дата: {_fmt_date(row.get('reply_at')) or '—'}")
        print(f"   Категория: {_category_label(row.get('reply_category', ''))}"
              + (" · передан менеджеру" if row.get("handed_to_manager_at") else ""))
        print(f"   id: {row['seller_id']}")
    print(f"\nВсего: {len(rows)}")
    return 0


def cmd_no_reply(args) -> int:
    store = SellerStore(DB_PATH)
    rows = store.comm_report_rows(replied=False, messaged=True, limit=args.limit)
    print(f"НЕ ОТВЕТИЛИ (писали, входящих нет): {len(rows)}")
    for row in rows:
        print(f"  {_shop(row):<30} отправлено: {_fmt_date(row.get('last_sent_at')) or '—'}"
              f"  id: {row['seller_id']}")
    return 0


def cmd_replies(args) -> int:
    store = SellerStore(DB_PATH)
    categories = None
    if args.category:
        key = args.category.lower().strip()
        if key not in REPLY_CATEGORY_LABELS:
            print(f"Неизвестная категория: {args.category}")
            print(f"Доступны: {', '.join(REPLY_CATEGORY_LABELS)}")
            return 1
        categories = [key]
    rows = store.comm_report_rows(replied=True, categories=categories, limit=args.limit)
    title = f"ОТВЕТЫ — {REPLY_CATEGORY_LABELS[categories[0]]}" if categories else "ВСЕ ОТВЕТЫ"
    print(f"{title}: {len(rows)}")
    for row in rows:
        print("\n" + "-" * 60)
        print_comm_card(row)
    return 0


def _find_seller(store: SellerStore, query: str):
    """Продавец по id, точному имени магазина или подстроке имени."""
    row = store.get_seller(query.strip())
    if row:
        return row
    exact = store.find_seller_by_shop(query)
    if exact:
        return exact
    needle = query.strip().lower()
    for candidate in store.iter_sellers():
        if needle in (candidate["shop_name"] or "").lower() \
                or needle in (candidate["company_name"] or "").lower():
            return candidate
    return None


def cmd_seller(args) -> int:
    store = SellerStore(DB_PATH)
    row = _find_seller(store, args.seller)
    if row is None:
        print(f"Продавец не найден: {args.seller}")
        return 1
    data = store.comm_report_rows(replied=None, limit=10_000)
    match = next((r for r in data if r["seller_id"] == row["seller_id"]), None)
    if match is None:
        match = dict(row)
        match.setdefault("last_reply_text", "")
        match.setdefault("last_sent_text", row.get("message_sent", ""))
        match.setdefault("last_sent_at", row.get("sent_at", ""))
    print_comm_card(match)
    print()
    print("ИСТОРИЯ ПЕРЕПИСКИ (свежие сверху):")
    for m in store.message_history(row["seller_id"], limit=30):
        who = "-> мы" if m["result"] == "sent" else (
            "<- продавец" if m["result"] == "received" else f"({m['result']})")
        category = f" [{_category_label(m['category'])}]" if m["category"] else ""
        print(f"  {_fmt_date(m['sent_at']) or '—'}  {who}{category}")
        for line in (m["text"] or "").splitlines() or [""]:
            print(f"      {line[:160]}")
    return 0


def cmd_handover(args) -> int:
    store = SellerStore(DB_PATH)
    row = _find_seller(store, args.seller)
    if row is None:
        print(f"Продавец не найден: {args.seller}")
        return 1
    ok = store.hand_to_manager(row["seller_id"])
    if ok:
        print(f"Продавец {_shop(row)} передан менеджеру.")
        print("Он остался в общей базе; найдёте его:")
        print("  run_crm.py replies          — все, кто ответил")
        print("  дашборд -> карточка продавца — отметка «У МЕНЕДЖЕРА» и стадия")
        if args.note:
            store.set_note(row["seller_id"], args.note)
            print("Заметка добавлена (задача менеджера пишите в неё).")
    else:
        print("Не найден в базе — ничего не изменено.")
    return 0 if ok else 1


def cmd_recategorize(args) -> int:
    store = SellerStore(DB_PATH)
    changed = store.recategorize(overwrite=args.all)
    scope = "все ответы" if args.all else "ответы без категории"
    print(f"Пересчитано категорий ({scope}): {changed}")
    return 0


def cmd_backfill(args) -> int:
    """Дотягивает полный текст ответа из живой сессии Ozon.

    Для продавцов, помеченных «ответил» до появления полного разбора чата
    (или если тогда чат не открылся): открывает список чатов, находит чат
    по имени магазина и снимает переписку целиком. Перезапись — только
    если полный текст длиннее сохранённого.
    """
    from src.crm.inbox import InboxChecker

    store = SellerStore(DB_PATH)
    if args.seller:
        row = _find_seller(store, args.seller)
        if row is None:
            print(f"Продавец не найден: {args.seller}")
            return 1
        wanted = [row["seller_id"]]
    else:
        wanted = [r["seller_id"] for r in store.iter_sellers(status="replied")]
    if not wanted:
        print("Ответивших продавцов нет — дотягивать нечего.")
        return 0

    print(f"Дотягиваю полный текст для {len(wanted)} продавца(ов)…")
    print("Откроется Chrome с профилем парсера — нужна залогиненная сессия Ozon.")
    result = InboxChecker(store, data_dir=DATA_DIR).backfill_texts(wanted)

    for item in result["updated"]:
        text = item["text"].replace("\n", " ")
        print(f"  Обновлено: {item['shop']} — {len(text)} симв.: \"{text[:200]}\"")
    for item in result["shorter"]:
        text = item["text"].replace("\n", " ")
        print(f"  Без изменений: {item['shop']} — полный текст не длиннее сохранённого: "
              f"\"{text[:120]}\"")
    for shop in result["no_text"]:
        print(f"  Не получен: {shop} — текст из чата снять не удалось, осталась превью")
    for seller_id in result["missing"]:
        print(f"  Чат не найден в списке: {seller_id} (не в первых чатах или имя не совпало)")
    print(f"\nИтог: чатов в списке {result['chats']}, найдено {result['found']}, "
          f"обновлено {len(result['updated'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Отчёты по коммуникации с продавцами Ozon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="сводка: кому писали, кто ответил, категории")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("interested", help="заинтересованные продавцы")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_interested)

    p = sub.add_parser("no-reply", help="писали, но не ответили")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_no_reply)

    p = sub.add_parser("replies", help="ответы с полным текстом")
    p.add_argument("--category", help=f"категория: {', '.join(REPLY_CATEGORY_LABELS)}")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_replies)

    p = sub.add_parser("seller", help="карточка коммуникации одного продавца")
    p.add_argument("seller", help="seller_id или имя магазина")
    p.set_defaults(func=cmd_seller)

    p = sub.add_parser("handover", help="передать продавца менеджеру")
    p.add_argument("seller", help="seller_id или имя магазина")
    p.add_argument("--note", default="", help="заметка для менеджера")
    p.set_defaults(func=cmd_handover)

    p = sub.add_parser("recategorize", help="пересчитать категории классификатором")
    p.add_argument("--all", action="store_true",
                   help="пересчитать все ответы, а не только без категории")
    p.set_defaults(func=cmd_recategorize)

    p = sub.add_parser("backfill", help="дотянуть полный текст ответа из живой сессии")
    p.add_argument("seller", nargs="?", default="",
                   help="seller_id или имя магазина; без аргумента — все ответившие")
    p.set_defaults(func=cmd_backfill)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
