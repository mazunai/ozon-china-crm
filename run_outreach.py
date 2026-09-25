#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рассылка китайским продавцам Ozon — точка входа.

    python run_outreach.py check             # проверить готовность к отправке
    python run_outreach.py score --top 30    # показать оценку, лучшие — в конце
    python run_outreach.py preview           # показать готовый текст сообщения
    python run_outreach.py inspect --limit 1 # снять разметку чата (один раз)
    python run_outreach.py send --once       # одна отправка (проверка настройки)
    python run_outreach.py send              # рабочий цикл: 1 сообщение в час
    python run_outreach.py stats             # состояние очереди
    python run_outreach.py optout <seller_id># больше никогда не писать этому продавцу

По умолчанию отправка идёт в режиме предпросмотра (`dry_run` в
outreach_data/sender.json). Чтобы реально отправлять — поставьте там false.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import Settings
from src.outreach.inspect_chat import inspect_chat
from src.crm.store import SellerStore
from src.outreach.sender import preflight as send_preflight
from src.outreach.scoring import (
    DEFAULT_WEIGHTS,
    ScoreWeights,
    SellerMetrics,
    format_report,
    score_all,
    sort_sellers,
)
from src.outreach.sender import SenderConfig, run_queue
from src.outreach.templates import (
    PLACEHOLDER_MARKER,
    TemplateError,
    list_templates,
    load_template,
    preview,
)
from src.utils.logger import setup_logging

logger = logging.getLogger(__name__)

DATA_DIR = Settings.BASE_DIR / "outreach_data"
TEMPLATES_DIR = DATA_DIR / "templates"
CONFIG_FILE = DATA_DIR / "sender.json"
# Отправляем из ОБЩЕЙ базы продавцов. Раньше здесь был outreach_data/queue.sqlite3,
# который создавался только командой `import` — а в дашборде и CRM данные жили в
# data/sellers.sqlite3. То есть отправка смотрела в пустой файл, и слать было
# физически некому.
DB_PATH = Settings.BASE_DIR / "data" / "sellers.sqlite3"


# --- Импорт из результатов парсера ---------------------------------------


def collect_sellers(output_dir: Path) -> List[SellerMetrics]:
    """Собирает продавцов из всех JSON-файлов парсера.

    Один продавец встречается во многих товарах — берём его один раз,
    метрики у повторов одинаковые, а ссылку на товар запоминаем первую
    попавшуюся: она нужна, чтобы открыть чат.
    """
    by_id = {}
    files = sorted(output_dir.glob("*/*.json")) + sorted(output_dir.glob("*.json"))

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"Пропускаю {path.name}: {exc}")
            continue

        for product in payload.get("products") or []:
            seller = product.get("seller") or {}
            seller_id = str(seller.get("id") or "").strip()
            if not seller_id:
                continue

            existing = by_id.get(seller_id)
            if existing:
                if not existing.sample_product_url and product.get("product_url"):
                    existing.sample_product_url = product["product_url"]
                continue

            by_id[seller_id] = SellerMetrics.from_raw(
                seller_id=seller_id,
                orders_count=seller.get("orders_count", ""),
                reviews_count=seller.get("reviews_count", ""),
                working_time=seller.get("working_time", ""),
                average_rating=seller.get("average_rating", ""),
                shop_name=seller.get("name", ""),
                company_name=seller.get("company_name", ""),
                is_chinese=bool(product.get("is_chinese")),
                province=product.get("seller_province", "") or "",
                uscc=product.get("seller_uscc", "") or "",
                sample_product_url=product.get("product_url", "") or "",
            )

    return score_all(by_id.values())


# --- Команды --------------------------------------------------------------



def cmd_score(args) -> int:
    sellers = collect_sellers(Path(args.output_dir))
    chinese = [s for s in sellers if s.is_chinese]
    if args.all:
        chinese = [s for s in sellers]

    weights = ScoreWeights()
    if args.weights:
        weights = ScoreWeights(**json.loads(args.weights))
    score_all(chinese, weights)

    ordered = sort_sellers(chinese, best_last=not args.best_first)
    print(f"Веса: {weights.as_dict()}")
    print(format_report(ordered, top=args.top))
    if ordered:
        print(f"\nЛучший: {ordered[-1].shop_name or ordered[-1].seller_id} (score={ordered[-1].score:.2f})")
    print(f"\nПо умолчанию веса: {DEFAULT_WEIGHTS.as_dict()}")
    return 0


def cmd_preview(args) -> int:
    paths = list_templates(TEMPLATES_DIR)
    if not paths:
        print(
            f"В {TEMPLATES_DIR} нет ни одного шаблона.\n"
            "Скопируйте заготовку и напишите свой текст:\n"
            f"    cp {TEMPLATES_DIR / 'example.txt.example'} {TEMPLATES_DIR / 'main.txt'}\n"
            "Переменные: {shop_name}, {company_name}, {orders}, {orders_word},\n"
            "{reviews}, {reviews_word}, {tenure}, {months}, {years}, {rating}, {province}.\n"
            "Подробности и образец структуры — внутри самой заготовки."
        )
        return 1

    template = load_template(Path(args.template) if args.template else paths[0])
    queue = SellerStore(DB_PATH)
    print(preview(template, queue, seller_id=args.seller))
    return 0


def cmd_inspect(args) -> int:
    queue = SellerStore(DB_PATH)
    rows = [r for r in queue.iter_sellers() if r["verified_by"] in ("uscc", "warehouse")]
    if not rows:
        print("В базе нет подтверждённых продавцов. Сначала выполните: run_harvest.py harvest")
        return 1

    row = rows[0]
    print(f"Открываю магазин: {row['shop_name'] or row['seller_id']}")
    print(f"URL: https://www.ozon.ru/seller/{row['seller_id']}/\n")
    print("Войдите в Ozon в открывшемся окне, если ещё не вошли.\n")

    report = inspect_chat(row["seller_id"], DATA_DIR)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])

    if not report.get("logged_in"):
        print("\n⚠ Сессия не залогинена — селекторы сняты с гостевой страницы, они не подойдут.")
        return 2

    print("\nПроверьте outreach_data/selectors.proposed.json глазами, затем скопируйте")
    print("нужные значения в outreach_data/selectors.json — отправщик читает только его.")
    return 0


def cmd_check(args) -> int:
    """Показывает, всё ли готово к отправке. Ничего не отправляет."""
    config = SenderConfig.load(CONFIG_FILE)
    paths = list_templates(TEMPLATES_DIR)
    template = None
    if paths:
        try:
            template = load_template(paths[0])
        except TemplateError as exc:
            print(f"Шаблон не готов: {exc}")
    if template is None and not paths:
        print(f"Шаблонов нет в {TEMPLATES_DIR}")
        print("Скопируйте заготовку: cp outreach_data/templates/example.txt.example "
              "outreach_data/templates/main.txt")

    report = send_preflight(config, template, DATA_DIR, store=SellerStore(DB_PATH))
    print(f"Готовность: {'ГОТОВО' if report['ready'] else 'НЕ ГОТОВО'}")
    print(f"Режим: {'предпросмотр (не отправляем)' if config.dry_run else 'РЕАЛЬНАЯ ОТПРАВКА'}")
    print()
    for check in report["checks"]:
        mark = "OK " if check["ok"] else ("БЛОК" if check["blocking"] else "нет")
        print(f"  [{mark}] {check['title']}: {check['detail']}")
    return 0 if report["ready"] else 1


def cmd_send(args) -> int:
    paths = list_templates(TEMPLATES_DIR)
    if not paths:
        print(f"В {TEMPLATES_DIR} нет шаблонов — отправлять нечего.")
        return 1
    template = load_template(Path(args.template) if args.template else paths[0])

    config = SenderConfig.load(CONFIG_FILE)
    if args.dry_run:
        config.dry_run = True
    if args.live:
        config.dry_run = False
    if args.interval:
        config.interval_minutes = args.interval
    if args.cap:
        config.daily_cap = args.cap

    if template.is_placeholder() and not config.dry_run:
        print(
            f"\nШаблон '{template.name}' — это ещё заготовка (в тексте осталась метка\n"
            f"«{PLACEHOLDER_MARKER}»). Напишите свой текст, прежде чем включать реальную отправку.\n"
            "Предпросмотр доступен всегда: добавьте --dry-run."
        )
        return 1

    print(f"Шаблон: {template.name}")
    print(f"Интервал: {config.interval_minutes} мин (±{config.interval_jitter_minutes})")
    print(f"Дневной лимит: {config.daily_cap}, окно: {config.window_start}–{config.window_end}")
    print(f"Режим: {'ПРЕДПРОСМОТР (не отправляем)' if config.dry_run else 'РЕАЛЬНАЯ ОТПРАВКА'}")

    queue = SellerStore(DB_PATH)
    print(f"Очередь: {queue.stats().as_text()}\n")

    report = run_queue(
        queue=queue,
        template=template,
        config=config,
        data_dir=DATA_DIR,
        once=args.once,
        on_progress=print,
    )
    print(f"\nИтог: отправлено={report['sent']}, ошибок={report['failed']}")
    if report["stopped_reason"]:
        print(f"Остановка: {report['stopped_reason']}")
    return 0


def cmd_stats(args) -> int:
    queue = SellerStore(DB_PATH)
    print(queue.stats().as_text())
    if args.list:
        for row in queue.iter_sellers(status=args.status):
            print(
                f"  {row['score']:>7.2f}  {row['status']:<10}  "
                f"{row['shop_name'] or row['seller_id']}"
            )
    return 0


def cmd_optout(args) -> int:
    queue = SellerStore(DB_PATH)
    queue.mark_opted_out(args.seller_id)
    print(f"{args.seller_id} добавлен в стоп-лист и больше не получит сообщений.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Рассылка китайским продавцам Ozon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_output_dir(p):
        p.add_argument(
            "--output-dir", default=str(Settings.OUTPUT_DIR),
            help="каталог с результатами парсера (по умолчанию output/)",
        )

    p_score = sub.add_parser("score", help="показать оценку продавцов")
    add_output_dir(p_score)
    p_score.add_argument("--top", type=int, default=30, help="сколько строк показать")
    p_score.add_argument("--best-first", action="store_true", help="лучшие сверху")
    p_score.add_argument("--all", action="store_true", help="включая не-китайских")
    p_score.add_argument("--weights", help='JSON с весами, напр. {"orders":1.5}')
    p_score.set_defaults(func=cmd_score)

    p_preview = sub.add_parser("preview", help="показать готовый текст сообщения")
    p_preview.add_argument("--template", help="путь к шаблону")
    p_preview.add_argument("--seller", help="seller_id для предпросмотра")
    p_preview.set_defaults(func=cmd_preview)

    p_inspect = sub.add_parser("inspect", help="снять разметку чата Ozon (один раз)")
    p_inspect.set_defaults(func=cmd_inspect)

    p_check = sub.add_parser("check", help="проверить готовность к отправке")
    p_check.set_defaults(func=cmd_check)

    p_send = sub.add_parser("send", help="запустить рассылку")
    p_send.add_argument("--template", help="путь к шаблону")
    p_send.add_argument("--once", action="store_true", help="одна отправка и выход")
    p_send.add_argument("--live", action="store_true", help="реально отправлять")
    p_send.add_argument("--dry-run", action="store_true", help="только показать")
    p_send.add_argument("--interval", type=int, help="интервал в минутах")
    p_send.add_argument("--cap", type=int, help="дневной лимит")
    p_send.set_defaults(func=cmd_send)

    p_stats = sub.add_parser("stats", help="состояние очереди")
    p_stats.add_argument("--list", action="store_true", help="показать продавцов")
    p_stats.add_argument("--status", help="фильтр по статусу")
    p_stats.set_defaults(func=cmd_stats)

    p_optout = sub.add_parser("optout", help="добавить продавца в стоп-лист")
    p_optout.add_argument("seller_id")
    p_optout.set_defaults(func=cmd_optout)

    return parser


def main() -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        return args.func(args)
    except TemplateError as exc:
        print(f"\nОшибка шаблона:\n{exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"\n{exc}")
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
