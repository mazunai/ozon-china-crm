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

Несколько менеджеров: реестр аккаунтов в outreach_data/accounts.json.

    python run_outreach.py accounts --add mgr2 --label "Менеджер 2"
    python run_outreach.py login --account mgr2   # вход в аккаунт (один раз)
    python run_outreach.py send --account mgr2    # рассылка от его имени

Каждый аккаунт — свой процесс со своим профилем Chrome, дневным лимитом и
стоп-краном; очередь в базе общая и размечает продавцов атомарно.

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
from src.outreach.accounts import (
    SenderAccount,
    add_account,
    get_account,
    load_accounts,
    remove_account,
)
from src.outreach.inspect_chat import inspect_chat
from src.crm.store import SellerStore
from src.outreach.sender import check_login_live, login_flow
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
    TemplatePool,
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

    queue = SellerStore(DB_PATH)
    if args.template:
        template = load_template(Path(args.template))
    else:
        # Без --template показываем тот же текст, который уйдёт следующим:
        # пул выбирает его по журналу отправок, поэтому предпросмотр не
        # расходится с тем, что реально уйдёт.
        pool = TemplatePool.load(TEMPLATES_DIR)
        template = pool.pick(queue.template_usage(), avoid=queue.last_template_used())

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


def cmd_login(args) -> int:
    """Открывает Chrome с профилем аккаунта для входа в Ozon.

    Нужно один раз на аккаунт: профиль парсера отдельный, сессия из обычного
    браузера в него не переносится. После входа cookies сохранятся, и отправка
    будет работать без него. Несколько менеджеров = login для каждого id.
    """
    try:
        account = get_account(DATA_DIR, getattr(args, "account", "") or "main")
    except ValueError as exc:
        print(f"{exc}")
        return 1

    print(f"Откроется Chrome с профилем аккаунта {account.id}.")
    print(f"Профиль: {account.profile_dir()}, порт {account.debug_port()}.")
    print("Войдите в Ozon в этом окне — окно само закроется, когда вход пройдёт.\n")

    def say(message: str) -> None:
        print(f"  {message}")

    ok = login_flow(DATA_DIR, timeout_seconds=args.timeout, on_progress=say,
                    account=account)
    if ok:
        print("\nПроверьте готовность: python run_outreach.py check"
              + (f" --account {account.id}" if account.id != "main" else ""))
        return 0
    print("\nВход не выполнен. Запустите команду снова и войдите в открывшемся окне.")
    return 1


def cmd_check(args) -> int:
    """Показывает, всё ли готово к отправке. Ничего не отправляет."""
    config = SenderConfig.load(CONFIG_FILE)
    try:
        account = get_account(DATA_DIR, getattr(args, "account", "") or "main")
    except ValueError as exc:
        print(f"{exc}")
        return 1
    if account.id != "main":
        config.sender_account_id = account.id
    account.apply_to_config(config)

    template = None
    try:
        template = TemplatePool.load(TEMPLATES_DIR)
    except TemplateError as exc:
        print(f"Шаблоны не готовы: {exc}")

    report = send_preflight(config, template, DATA_DIR, store=SellerStore(DB_PATH))

    # Живая проверка входа: без браузера её не сделать, поэтому по флагу
    if getattr(args, "live", False):
        print("Проверяю вход в Ozon (откроется окно браузера)…\n")
        live = check_login_live(DATA_DIR, on_progress=lambda m: print(f"  {m}"),
                                account=account)
        report["checks"].append({
            "key": "login", "ok": live["logged_in"],
            "title": f"Вход в Ozon (аккаунт {account.id})",
            "detail": "сессия активна" if live["logged_in"] else (live["error"] or "не подтверждён"),
            "blocking": not config.dry_run,
        })
        if not live["logged_in"]:
            report["ready"] = False

    print(f"Готовность: {'ГОТОВО' if report['ready'] else 'НЕ ГОТОВО'}")
    print(f"Режим: {'предпросмотр (не отправляем)' if config.dry_run else 'РЕАЛЬНАЯ ОТПРАВКА'}")
    if template is not None:
        drafts = [t.name for t in template if t.is_placeholder()]
        print(f"Тексты: {template.usage_report(SellerStore(DB_PATH).template_usage())}")
        if drafts:
            print(f"  черновики (в ротацию не идут): {', '.join(drafts)}")
    print()
    for check in report["checks"]:
        mark = "OK " if check["ok"] else ("БЛОК" if check["blocking"] else "нет")
        print(f"  [{mark}] {check['title']}: {check['detail']}")
    return 0 if report["ready"] else 1


def cmd_send(args) -> int:
    queue = SellerStore(DB_PATH)
    if args.template:
        template = TemplatePool([load_template(Path(args.template))])
    else:
        try:
            template = TemplatePool.load(TEMPLATES_DIR)
        except TemplateError as exc:
            print(f"\n{exc}")
            return 1

    config = SenderConfig.load(CONFIG_FILE)
    try:
        account = get_account(DATA_DIR, getattr(args, "account", "") or "main")
    except ValueError as exc:
        print(f"{exc}")
        return 1
    # Аккаунт процесса: журнал помечается его id, лимиты — его (переопреде-
    # ления аккаунта поверх общих настроек sender.json).
    config.sender_account_id = account.id
    account.apply_to_config(config)
    if args.dry_run:
        config.dry_run = True
    if args.live:
        config.dry_run = False
    if args.interval:
        config.interval_minutes = args.interval
    if args.cap:
        config.daily_cap = args.cap

    # Заготовку не отправляем даже по прямой команде: недописанный текст
    # уходит в ротацию только когда с него снята метка.
    if template.is_placeholder() and not config.dry_run:
        print(
            "\nВсе тексты — ещё заготовки (в них осталась метка\n"
            f"«{PLACEHOLDER_MARKER}»). Напишите хотя бы один, прежде чем включать\n"
            "реальную отправку. Предпросмотр доступен всегда: добавьте --dry-run."
        )
        return 1

    print(f"Тексты ({template.name}):")
    print(template.usage_report(queue.template_usage(), queue.template_performance() or None))
    print(f"Аккаунт: {account.id}"
          + (f" ({account.label})" if account.label else "")
          + f" — профиль {account.profile_dir()}, порт {account.debug_port()}")
    print(f"Интервал: {config.interval_minutes} мин (±{config.interval_jitter_minutes})")
    print(f"Дневной лимит: {config.daily_cap}, окно: {config.window_start}–{config.window_end}")
    print(f"Режим: {'ПРЕДПРОСМОТР (не отправляем)' if config.dry_run else 'РЕАЛЬНАЯ ОТПРАВКА'}")

    print(f"Очередь: {queue.stats().as_text()}\n")

    report = run_queue(
        queue=queue,
        template=template,
        config=config,
        data_dir=DATA_DIR,
        once=args.once,
        on_progress=print,
        limit=getattr(args, "limit", None),
        account=account,
    )
    print(f"\nИтог: отправлено={report['sent']}, ошибок={report['failed']}")
    if report["stopped_reason"]:
        print(f"Остановка: {report['stopped_reason']}")
    return 0


def cmd_templates(args) -> int:
    """Показывает тексты в каталоге и сколько раз каждый уже ушёл.

    Нужно, чтобы видеть раскладку ротации: какие варианты уже проверены на
    живых продавцах, а какие ещё нет.
    """
    try:
        pool = TemplatePool.load(TEMPLATES_DIR)
    except TemplateError as exc:
        print(f"\n{exc}")
        return 1

    store = SellerStore(DB_PATH)
    usage = store.template_usage()
    performance = store.template_performance()
    usable = pool.usable()

    print(f"Каталог: {TEMPLATES_DIR}")
    print(f"Текстов всего: {len(pool)}", end="")
    if len(usable) != len(pool):
        print(f", участвуют в ротации: {len(usable)} (остальные — черновики)")
    else:
        print()
    print()
    print(pool.usage_report(usage, performance or None))
    print(f"\nСледующим уйдёт: {pool.pick(usage, avoid=store.last_template_used(), performance=performance or None).name}")
    if not usage:
        print("Пока ничего не отправлено — каждый вариант сначала набирает статистику,")
        print(f"потом чаще уходят те, на которые отвечают (порог: {TemplatePool.AB_MIN_EXPOSURE} отправок).")
    return 0


def cmd_accounts(args) -> int:
    """Показывает реестр менеджеров или добавляет/убирает аккаунт.

    Аккаунт — отдельная сессия Chrome (свой профиль и порт) со своими
    лимитами, стабильным отпечатком и резидентным прокси. После добавления:
    login --account <id>, затем send --account <id>.
    """
    if args.add:
        try:
            account = add_account(DATA_DIR, args.add, label=args.label or "", proxy=getattr(args, "proxy", "") or "")
        except ValueError as exc:
            print(f"{exc}")
            return 1
        fp = account.get_fingerprint()
        print(f"Аккаунт {account.id} добавлен (порт {account.debug_port()}).")
        print(f"Отпечаток зафиксирован: GPU={fp.webgl_renderer}, OS={fp.platform}, разрешение={fp.screen_width}x{fp.screen_height}")
        if account.proxy:
            print(f"Привязан прокси: {account.proxy}")
        print(f"Дальше: python run_outreach.py login --account {account.id}")
        return 0

    if args.remove:
        try:
            remove_account(DATA_DIR, args.remove)
        except ValueError as exc:
            print(f"{exc}")
            return 1
        print(f"Аккаунт {args.remove} убран из реестра "
              "(профиль Chrome на диске остался — удалить руками, если нужен).")
        return 0

    store = SellerStore(DB_PATH)
    accounts = load_accounts(DATA_DIR)
    print(f"Реестр: {DATA_DIR / 'accounts.json'} — аккаунтов: {len(accounts)}\n")
    for acc in accounts:
        sent = store.sent_today(acc.id)
        profile_exists = "профиль есть" if acc.profile_dir().exists() else "профиля ещё нет"
        fp = acc.get_fingerprint()
        proxy_str = f"прокси: {acc.proxy}" if acc.proxy else "прокси: прямой"
        print(f"  {acc.id:<10} {acc.label:<16} порт {acc.debug_port():<5} "
              f"{profile_exists:<14} GPU: {fp.webgl_vendor.split('(')[-1].rstrip(')'):<7} "
              f"{proxy_str:<22} сегодня: {sent}")
    print("\nЗапуск аккаунта:  python run_outreach.py send --account <id>")
    print("Вход аккаунта:    python run_outreach.py login --account <id>")
    print("Проверка аккаунта: python run_outreach.py check --account <id> --live")
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

    p_accounts = sub.add_parser("accounts", help="список аккаунтов менеджеров")
    p_accounts.add_argument("--add", help="добавить аккаунт с таким id (например mgr2)")
    p_accounts.add_argument("--label", help="подпись аккаунта (например «Менеджер 2»)")
    p_accounts.add_argument("--proxy", help="резидентный прокси (например http://user:pass@host:port)")
    p_accounts.add_argument("--remove", help="убрать аккаунт из реестра")
    p_accounts.set_defaults(func=cmd_accounts)

    p_login = sub.add_parser("login", help="войти в Ozon в профиле аккаунта")
    p_login.add_argument("--account", default="main",
                         help="id аккаунта из accounts.json (по умолчанию main)")
    p_login.add_argument("--timeout", type=int, default=300,
                         help="сколько секунд ждать вход")
    p_login.set_defaults(func=cmd_login)

    p_check = sub.add_parser("check", help="проверить готовность к отправке")
    p_check.add_argument("--account", default="main",
                         help="id аккаунта из accounts.json (по умолчанию main)")
    p_check.add_argument("--live", action="store_true",
                         help="дополнительно проверить вход в Ozon (откроет браузер)")
    p_check.set_defaults(func=cmd_check)

    p_send = sub.add_parser("send", help="запустить рассылку")
    p_send.add_argument("--account", default="main",
                        help="id аккаунта из accounts.json: свой профиль, лимит и стоп-кран")
    p_send.add_argument("--template", help="путь к шаблону")
    p_send.add_argument("--once", action="store_true", help="одна отправка и выход")
    p_send.add_argument("--live", action="store_true", help="реально отправлять")
    p_send.add_argument("--dry-run", action="store_true", help="только показать")
    p_send.add_argument("--interval", type=int, help="интервал в минутах")
    p_send.add_argument("--cap", type=int, help="дневной лимит")
    p_send.add_argument("--limit", type=int,
                        help="отправить N сообщений и остановиться (для проверки)")
    p_send.set_defaults(func=cmd_send)

    p_templates = sub.add_parser("templates", help="список текстов и сколько раз каждый ушёл")
    p_templates.set_defaults(func=cmd_templates)

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
