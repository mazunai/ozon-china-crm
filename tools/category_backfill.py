#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ретро-проход: категории ниш для продавцов, уже лежащих в базе.

Зачем: категорию товара мы снимаем с карточки при обычном сборе (см.
src/crm/category.py), но база собрана ДО этой функции. Скрипт проходится по
продавцам, у которых есть товар-пример, открывает одну карточку на продавца
и дописывает замер в журнал seller_categories.

Правила безопасности:
* существующие таблицы и колонки не пишутся вообще — только INSERT в
  seller_categories (история измерений сохраняется целиком);
* продавцы, у кого замер уже есть, пропускаются (повтор — только с --refresh);
* темп задаётся --delay, сессия браузера общая с дашбордом.

    # проверка: один продавец
    python tools/category_backfill.py --seller 164285

    # партия на 50 продавцов
    python tools/category_backfill.py --limit 50

    # полный проход фоном (остановить: коснуться data/category_backfill.stop)
    nohup python tools/category_backfill.py >> logs/category_backfill.log 2>&1 &
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
STOP_PATH = DATA_DIR / "category_backfill.stop"
LOCK_PATH = DATA_DIR / "category_backfill.lock"
LOG_DIR = REPO_ROOT / "logs"

ARTICLE_RE = re.compile(r"/product/(?:[^/]*?)(\d+)/?")


def log(message: str, log_file: Path) -> None:
    line = f"[{datetime.now().strftime('%d.%m %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def already_running() -> int:
    if not LOCK_PATH.exists():
        return 0
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
    except OSError:
        return 0
    return pid


def take_lock() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def article_from_url(url: str) -> str:
    match = ARTICLE_RE.search(url or "")
    return match.group(1) if match else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ретро-проход: категории ниш для продавцов из базы")
    parser.add_argument("--delay", type=float, default=1.2,
                        help="пауза между запросами, сек (по умолчанию 1.2)")
    parser.add_argument("--limit", type=int, default=0,
                        help="сколько продавцов обработать (0 — все без категории)")
    parser.add_argument("--refresh", action="store_true",
                        help="переснять категории даже у тех, у кого они есть")
    parser.add_argument("--seller", default="",
                        help="обработать одного продавца (для проверки)")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8000",
                        help="адрес дашборда для проверки занятости браузера")
    return parser


def dashboard_busy(base_url: str) -> str:
    """Занят ли браузер дашбордом: 'сбор', 'рассылка' или ''.

    Ночной сбор не проверяем: этот скрипт и есть ретро-проход той же природы.
    Проверки повторяют night_harvest.dashboard_busy.
    """
    import http.cookiejar
    import urllib.parse
    import urllib.request

    user = os.environ.get("OZON_DASHBOARD_USER", "")
    password = os.environ.get("OZON_DASHBOARD_PASSWORD", "")
    env_path = REPO_ROOT / ".env"
    if env_path.exists() and (not user or not password):
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "OZON_DASHBOARD_USER":
                user = value.strip()
            elif key == "OZON_DASHBOARD_PASSWORD":
                password = value.strip()
    if not password:
        return ""

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"username": user, "password": password}).encode("utf-8")
    try:
        opener.open(base_url.rstrip("/") + "/api/login", data=data, timeout=10).read()
    except Exception:                                       # noqa: BLE001
        return ""

    def get_json(path: str) -> dict:
        try:
            with opener.open(base_url.rstrip("/") + path, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:                                   # noqa: BLE001
            return {}

    if get_json("/api/harvest").get("running"):
        return "сбор"
    if get_json("/api/night").get("running"):
        return "ночной сбор"
    if (get_json("/api/send").get("state") or {}).get("running"):
        return "рассылка"
    return ""


def main() -> int:
    args = build_parser().parse_args()

    busy_pid = already_running()
    if busy_pid:
        print(f"Ретро-проход уже идёт (pid {busy_pid})")
        return 0
    take_lock()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"category_backfill_{datetime.now().strftime('%Y-%m-%d')}.log"

    from src.crm.category import extract_category
    from src.crm.store import SellerStore
    from src.utils.browser import create_browser_manager

    store = SellerStore(DATA_DIR / "sellers.sqlite3")
    done_ids = set() if args.refresh else store.categorized_seller_ids()

    if args.seller:
        rows = [r for r in store.iter_sellers() if r["seller_id"] == args.seller]
    else:
        rows = store.iter_sellers()
        if not args.refresh:
            rows = [r for r in rows if r["seller_id"] not in done_ids]

    pending = []
    for row in rows:
        article = article_from_url(row["sample_product_url"])
        if article:
            pending.append((row["seller_id"], article, row["shop_name"]))
    if args.limit and args.limit > 0:
        pending = pending[:args.limit]

    log(f"старт: к обработке {len(pending)} продавцов, пауза {args.delay} с", log_file)
    if not pending:
        log("обрабатывать нечего — у всех есть замеры", log_file)
        release_lock()
        return 0

    manager = None
    extracted = missed = 0
    try:
        manager = create_browser_manager(headless=True)
        driver = manager.create_driver()
        driver.get("https://www.ozon.ru/")
        client_args = {"delay": args.delay, "adaptive": True}
        from src.crm.harvest import OzonPageClient

        client = OzonPageClient(driver, **client_args)

        for index, (seller_id, article, shop_name) in enumerate(pending, 1):
            if STOP_PATH.exists():
                log("найден стоп-файл — завершаю", log_file)
                break
            busy = dashboard_busy(args.dashboard)
            if busy:
                log(f"браузер занят ({busy} в дашборде) — жду 60 с", log_file)
                time.sleep(60)
            data = client.page(f"/product/{article}/")
            category = extract_category(data or {})
            if category:
                store.record_category(seller_id, article, category)
                extracted += 1
                if index % 25 == 0 or index == len(pending):
                    log(f"обработано {index}/{len(pending)}: "
                        f"с категорией {extracted}, без {missed} "
                        f"(последняя: {shop_name} → {category['leaf']})", log_file)
            else:
                missed += 1
                log(f"{index}/{len(pending)}: {shop_name} — категории нет в карточке", log_file)
    finally:
        release_lock()
        if manager is not None:
            try:
                manager.close()
            except Exception:                                # noqa: BLE001
                pass
        log(f"финиш: с категорией {extracted}, без {missed}", log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
