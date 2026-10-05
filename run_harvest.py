#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор китайских продавцов Ozon в базу — точка входа.

    # Собрать продавцов из раздела «Товары из Китая»
    python run_harvest.py harvest --url "https://www.ozon.ru/highlight/tovary-iz-kitaya-935133/" --pages 10

    # Посмотреть, что в базе
    python run_harvest.py list --limit 30
    python run_harvest.py stats

    # Пометить вручную
    python run_harvest.py note 2948681 "написал 25.09, ждём ответ"
    python run_harvest.py status 2948681 replied

    # Выгрузить в CSV
    python run_harvest.py export --out китайские_продавцы.csv

Ссылку можно вставлять прямо из браузера, вместе с хвостом `?at=...&sh=...` —
лишние параметры отбрасываются, а `page` сохраняется.
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import Settings
from src.crm.harvest import DEFAULT_DELAY, ChinaSellerHarvester, OzonPageClient
from src.crm.verify import build_widget_request, find_delivery_node, verify_by_warehouse
from src.crm.store import (
    STATUS_LABELS,
    VERIFY_LABELS,
    SellerStore,
)
from src.crm.verify import CN_WAREHOUSE_RE  # noqa: F401  (для справки в --help)
from src.utils.logger import setup_logging

logger = logging.getLogger(__name__)

DATA_DIR = Settings.BASE_DIR / "data"
DB_PATH = DATA_DIR / "sellers.sqlite3"

# Раздел Ozon Global с товарами из Китая. Проверено: товары там китайские
# (11 из 11 на страницах 1, 2, 3 и 50).
DEFAULT_SECTION = "GLOBAL"


def open_store() -> SellerStore:
    return SellerStore(DB_PATH)


def _driver():
    """Браузер для запросов. Ozon блокирует обычные HTTP-клиенты."""
    from src.utils.browser import create_browser_manager

    manager = create_browser_manager(headless=True)
    driver = manager.create_driver()
    return manager, driver


def cmd_harvest(args) -> int:
    store = open_store()
    manager, driver = None, None
    try:
        manager, driver = _driver()
        # Открываем Ozon один раз: дальше запросы идут fetch'ем из страницы,
        # это на порядок быстрее навигации на каждый URL.
        driver.get("https://www.ozon.ru/")
        # Адаптивная пауза по умолчанию; явный --delay означает фиксированный
        # темп (пользователь сам выбрал значение).
        adaptive = args.delay == DEFAULT_DELAY
        client = OzonPageClient(driver, delay=args.delay, adaptive=adaptive)

        harvester = ChinaSellerHarvester(
            client=client, store=store, on_progress=print,
            refresh_days=args.refresh_days,
        )
        stats = harvester.run(
            args.url or DEFAULT_SECTION, pages=args.pages, start_page=args.start_page,
        )

        print(f"\nИтог: {stats.as_text()}")
        if stats.errors:
            print(f"Ошибок: {len(stats.errors)} (первые 5)")
            for error in stats.errors[:5]:
                print(f"  - {error}")
        # Машинный итог для ночного автосбора (tools/night_harvest.py): по нему
        # решается, идти ли дальше по страницам или раздел кончился.
        if args.json:
            print("HARVEST_JSON " + json.dumps({
                "pages": stats.pages,
                "products": stats.products,
                "sellers": stats.sellers_found,
                "uscc": stats.sellers_verified_uscc,
                "warehouse": stats.sellers_verified_warehouse,
                "rejected": stats.sellers_rejected,
                "errors": len(stats.errors),
                "cancelled": stats.cancelled,
            }, ensure_ascii=False))
        return 0
    except Exception as exc:                                # noqa: BLE001
        logger.exception("Сбор не удался")
        print(f"\nОшибка: {exc}")
        return 1
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass


def cmd_list(args) -> int:
    store = open_store()
    rows = store.list_sellers(
        verified_only=not args.all,
        statuses=[args.status] if args.status else None,
        search=args.search or "",
        order_by=args.order_by,
        descending=not args.asc,
        limit=args.limit,
        offset=args.offset,
    )
    total = store.count_sellers(verified_only=not args.all, search=args.search or "")
    if not rows:
        print("Ничего не найдено. Запустите: python run_harvest.py harvest")
        return 0

    print(f"Показано {len(rows)} из {total}\n")
    header = f"{'score':>7}  {'заказы':>9}  {'отзывы':>9}  {'мес':>4}  {'оцен':>5}  {'провинция':<10}  {'статус':<11}  магазин"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['score']:>7.2f}  {row['orders']:>9,}  {row['reviews']:>9,}  "
            f"{row['months_on_ozon']:>4}  {row['rating']:>5.1f}  "
            f"{(row['province'] or '—'):<10}  {(STATUS_LABELS.get(row['status'], row['status'])):<11}  "
            f"{row['shop_name'] or row['seller_id']}"
        )
    print(f"\nПрофиль продавца: python run_harvest.py show <seller_id>")
    return 0


def cmd_show(args) -> int:
    store = open_store()
    row = store.get_seller(args.seller_id)
    if not row:
        print(f"Продавец {args.seller_id} не найден")
        return 1

    data = dict(row)
    print(f"Магазин:          {data['shop_name'] or '—'}")
    print(f"seller_id:        {data['seller_id']}")
    print(f"Компания:         {data['company_name'] or '—'}")
    print(f"Регистрация:      {data['registration_kind']} {data['uscc'] or ''}".rstrip())
    print(f"Провинция:        {data['province'] or '—'}")
    print(f"Достоверность:    {VERIFY_LABELS.get(data['verified_by'], data['verified_by'])}")
    print(f"Адрес:            {data['address'] or '—'}")
    print(f"Заказов:          {data['orders']:,}")
    print(f"Отзывов:          {data['reviews']:,}")
    print(f"Работает месяцев: {data['months_on_ozon']}")
    print(f"Рейтинг:          {data['rating']}")
    print(f"Оценка:           {data['score']:.2f}")
    print(f"Статус:           {STATUS_LABELS.get(data['status'], data['status'])}")
    print(f"Заметка:          {data['notes'] or '—'}")
    print(f"Профиль:          {data['seller_profile_url'] or '—'}")
    print(f"Товар:            {data['sample_product_url'] or '—'}")
    if data["chinese_reasons"]:
        print(f"Причины:          {data['chinese_reasons']}")
    return 0


def cmd_stats(args) -> int:
    store = open_store()
    print(store.stats().as_text())
    if args.by_verification:
        print("\nПо достоверности:")
        for key, value in sorted(store.stats().by_verification.items()):
            print(f"  {VERIFY_LABELS.get(key, key):<22} {value}")
    return 0


def cmd_note(args) -> int:
    store = open_store()
    if store.set_note(args.seller_id, args.text):
        print(f"Заметка сохранена для {args.seller_id}")
        return 0
    print(f"Продавец {args.seller_id} не найден")
    return 1


def cmd_status(args) -> int:
    store = open_store()
    try:
        ok = store.set_status(args.seller_id, args.status)
    except ValueError as exc:
        print(exc)
        return 1
    if ok:
        print(f"{args.seller_id}: статус -> {STATUS_LABELS.get(args.status, args.status)}")
        return 0
    print(f"Продавец {args.seller_id} не найден")
    return 1


def cmd_export(args) -> int:
    store = open_store()
    rows = store.export_rows(verified_only=not args.all)
    if not rows:
        print("База пуста — выгружать нечего")
        return 1

    out_path = Path(args.out)
    columns = [
        ("seller_id", "seller_id"),
        ("shop_name", "Магазин"),
        ("company_name", "Компания"),
        ("uscc", "USCC"),
        ("province", "Провинция"),
        ("orders", "Заказов"),
        ("reviews", "Отзывов"),
        ("months_on_ozon", "Месяцев на Ozon"),
        ("rating", "Рейтинг"),
        ("score", "Оценка"),
        ("verified_by", "Достоверность"),
        ("status", "Статус"),
        ("notes", "Заметка"),
        ("seller_profile_url", "Профиль продавца"),
        ("sample_product_url", "Товар"),
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow([title for _, title in columns])
        for row in rows:
            writer.writerow([row[key] for key, _ in columns])

    print(f"Выгружено {len(rows)} продавцов в {out_path}")
    return 0


def cmd_reverify(args) -> int:
    """Перепроверяет продавцов, подтверждённых по складу, и записывает причину.

    Нужна потому, что склад — вторичный признак: он слабее USCC. Команда
    заново читает виджет доставки и либо записывает регион склада как
    обоснование, либо понижает продавца до «не подтверждён», если Китай не
    подтвердился.
    """
    import json as _json

    store = open_store()
    rows = [r for r in store.iter_sellers() if r["verified_by"] == "warehouse"]
    if not rows:
        print("Нет продавцов, подтверждённых по складу — перепроверять нечего.")
        return 0

    # Сначала — проверка юрлица, без сети. Российская регистрация (ООО/ИП,
    # ОГРН/ОГРНИП) перебивает склад: такая компания может везти из Китая, но
    # китайским продавцом не является. Это то, из-за чего в подтверждённые
    # попадали ООО "АРАВИЯ" и подобные.
    by_legal = [r for r in rows if r["registration_kind"] in ("ogrn", "ogrnip", "inn_ru")]
    rest = [r for r in rows if r["registration_kind"] not in ("ogrn", "ogrnip", "inn_ru")]

    for r in by_legal:
        store.set_verification_reason(
            r["seller_id"], "none",
            f"российское юрлицо ({r['registration_kind']}) — китайским продавцом "
            "не считается, даже если товар едет из Китая",
        )
    if by_legal:
        print(f"Понижено без сети по юрлицу: {len(by_legal)}")
        for r in by_legal:
            print(f"  {(r['shop_name'] or '—')[:30]:<31} {r['company_name'][:30]}")

    rows = rest
    if not rows:
        print("\nОстальные проверки не требуются.")
        return 0

    print(f"\nПерепроверяю {len(rows)} продавцов по виджету доставки…")
    manager, driver = None, None
    confirmed = downgraded = failed = 0
    try:
        manager, driver = _driver()
        client = OzonPageClient(driver, delay=args.delay)
        client.ensure_ready()

        for index, row in enumerate(rows, 1):
            url = row["sample_product_url"]
            if not url:
                failed += 1
                continue
            article = url.rstrip("/").split("/")[-1]
            card = client.page(f"/product/{article}/")
            if not card:
                failed += 1
                continue

            request = build_widget_request(
                find_delivery_node(card.get("layout")), f"/product/{article}/"
            )
            if not request:
                failed += 1
                continue
            response = client.fetch_json(request["url"], method="POST", body=request["body"])
            info = verify_by_warehouse(_json.dumps(response or {}, ensure_ascii=False))

            if info.is_china:
                confirmed += 1
                store.set_verification_reason(
                    row["seller_id"], "warehouse", f"склад: {info.reason}",
                    province=info.region,
                )
                mark = "OK  "
            else:
                downgraded += 1
                store.set_verification_reason(
                    row["seller_id"], "none",
                    f"склад не подтвердил Китай: {info.reason or 'регион не найден'}",
                )
                mark = "ПОНИЖЕН"
            print(f"  [{index}/{len(rows)}] {mark} {(row['shop_name'] or row['seller_id'])[:30]:<31} {info.reason}")

    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass

    print(f"\nПодтверждено: {confirmed}, понижено: {downgraded}, не удалось проверить: {failed}")
    if confirmed:
        print("Обнови страницу дашборда — причины склада теперь видны в карточке.")
    return 0


def cmd_purge(args) -> int:
    """Удаляет из базы записи, не подтверждённые как китайские.

    Нужна, если хочется, чтобы в базе не было вообще ничего лишнего: записи
    уровня «не подтверждён» и «эвристика» — это кандидаты, которые проверку не
    прошли, и среди них встречаются российские юрлица. В дашборде они и так
    скрыты, но физически лежат в файле и попадают в выгрузку «--all».

    Требует явного подтверждения: удаление необратимо.
    """
    store = open_store()
    rows = [r for r in store.iter_sellers()
            if r["verified_by"] not in ("uscc", "warehouse")]

    if not rows:
        print("Нечего удалять: все записи подтверждены.")
        return 0

    print(f"К удалению: {len(rows)} записей, не подтверждённых как китайские:\n")
    for r in rows[:20]:
        legal = (r["company_name"] or "—")[:34]
        print(f"  [{r['verified_by']:<9}] {(r['shop_name'] or '—')[:30]:<31} юрлицо: {legal}")
    if len(rows) > 20:
        print(f"  … и ещё {len(rows) - 20}")

    if not args.yes:
        print("\nЭто необратимая операция. Повтори с флагом --yes.")
        print("Перед удалением советую снять копию: python tools/backup_db.py")
        return 1

    deleted = store.delete_sellers([r["seller_id"] for r in rows])
    print(f"\nУдалено: {deleted}")
    print(store.stats().as_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сбор китайских продавцов Ozon в базу",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Раздел по умолчанию: {DEFAULT_SECTION}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("harvest", help="собрать продавцов из раздела")
    p.add_argument("--url", default=DEFAULT_SECTION, help="ссылка на раздел Ozon Global")
    p.add_argument("--pages", type=int, default=10, help="сколько страниц обойти (8 товаров на страницу)")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help="пауза между запросами, сек; при явной --delay адаптация отключается")
    p.add_argument("--refresh-days", type=int, default=7, metavar="N",
                   help="перечитывать модалки известных продавцов раз в N дней; "
                        "0 — никогда, только новые (по умолчанию 7)")
    p.add_argument("--start-page", type=int, default=1, metavar="N",
                   help="начать со страницы N, не заходя на предыдущие "
                        "(прыжок вглубь раздела для больших обходов чанками; по умолчанию 1)")
    p.add_argument("--json", action="store_true",
                   help="допечатать итог строкой HARVEST_JSON (для ночного автосбора)")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("list", help="показать продавцов из базы")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--search", help="поиск по магазину, компании, USCC и заметкам")
    p.add_argument("--status", choices=sorted(STATUS_LABELS), help="фильтр по статусу")
    p.add_argument("--order-by", default="score",
                   choices=["score", "orders", "reviews", "months_on_ozon", "rating", "shop_name", "status"])
    p.add_argument("--asc", action="store_true", help="по возрастанию (по умолчанию убывание)")
    p.add_argument("--all", action="store_true", help="включая неподтверждённых")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="подробности по продавцу")
    p.add_argument("seller_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("reverify",
                       help="перепроверить подтверждённых по складу и записать причину")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    p.set_defaults(func=cmd_reverify)

    p = sub.add_parser("purge",
                       help="удалить записи, не подтверждённые как китайские")
    p.add_argument("--yes", action="store_true", help="подтвердить удаление")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("stats", help="состояние базы")
    p.add_argument("--by-verification", action="store_true", help="разбивка по достоверности")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("note", help="сохранить заметку")
    p.add_argument("seller_id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("status", help="сменить статус вручную")
    p.add_argument("seller_id")
    p.add_argument("status", choices=sorted(STATUS_LABELS))
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="выгрузить в CSV")
    p.add_argument("--out", default="китайские_продавцы.csv")
    p.add_argument("--all", action="store_true", help="включая неподтверждённых")
    p.set_defaults(func=cmd_export)

    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
