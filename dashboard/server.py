#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-дашборд Ozon-парсера.

Поднимает локальный (или серверный) HTTP-сервер, через который можно:
  • запускать и останавливать парсинг;
  • смотреть живой лог и прогресс (Server-Sent Events);
  • смотреть результаты и фильтровать только китайских продавцов;
  • выгружать CSV.

Запуск:  python3 dashboard/server.py [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DASH_DIR = Path(__file__).resolve().parent
REPO_ROOT = DASH_DIR.parent
WEB_DIR = DASH_DIR / "web"
sys.path.insert(0, str(REPO_ROOT))

# Иконки и манифест отдаём без входа. Их запрашивает сам браузер — в том числе
# на странице входа и при «Добавить на главный экран», где cookie ни при чём.
PUBLIC_STATIC = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("icon-192.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}

DEFAULT_PORT = 8000

# База продавцов: мини-CRM с заметками и статусами
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "sellers.sqlite3"

# Wildberries: код воркера и CRM в проекте остаётся, но по умолчанию выключен
# (пока работаем чисто с Ozon — WB не мельтешит ни в UI, ни в API). Вернуть:
# WB_ENABLED=1 в .env и перезапуск дашборда — переключатель и карточка WB
# появятся сами, база sellers-wb.sqlite3 останется как была.
WB_ENABLED = (os.environ.get("WB_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")

# Учётные данные дашборда. Дашборд публикуется наружу через SSH-туннель,
# поэтому без пароля он открыт всему интернету. Пароль лежит в .env
# (файл в .gitignore), логин по умолчанию — admin.
ENV_PATH = REPO_ROOT / ".env"


def load_credentials() -> tuple:
    """Читает логин и пароль из окружения или из .env.

    Пароль обязателен при публикации наружу: дашборд отдаёт бизнес-данные, а
    SSH-туннель выставляет его в интернет. Для локальной отладки авторизацию
    можно снять явным флагом OZON_NO_AUTH=1 — «пустая переменная» для этого не
    годится, потому что тогда подставляется значение из .env.
    """
    import os

    if os.environ.get("OZON_NO_AUTH") == "1":
        return "admin", ""

    user = os.environ.get("OZON_DASHBOARD_USER", "")
    password = os.environ.get("OZON_DASHBOARD_PASSWORD", "")

    if (not user or not password) and ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "OZON_DASHBOARD_USER" and not user:
                    user = value.strip()
                elif key.strip() == "OZON_DASHBOARD_PASSWORD" and not password:
                    password = value.strip()
        except Exception:                                  # noqa: BLE001
            pass

    return user or "admin", password


# Поля, которые попадут в Excel-отчёт (китайские признаки — в первую очередь)
EXPORT_FIELDS = [
    "name", "company_name", "inn", "is_chinese", "chinese_confidence",
    "seller_country", "chinese_reasons", "price", "card_price",
    "product_url", "image_url", "orders_count", "reviews_count",
    "average_rating", "working_time",
]


# --------------------------------------------------------------------------- #
#  Доступ к базе продавцов (мини-CRM)
# --------------------------------------------------------------------------- #
class SellersApi:
    """Читает и правит базу продавцов для дашборда.

    Вся работа с SQLite инкапсулирована в `SellerStore`: дашборд не держит
    результаты в памяти, поэтому перезапуск сервера больше ничего не теряет.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._store = None
        self._lock = threading.Lock()

    def store(self):
        with self._lock:
            if self._store is None:
                from src.crm.store import SellerStore
                self._store = SellerStore(self.db_path)
            return self._store

    def sellers(self, query: dict) -> dict:
        """Список продавцов с фильтрами и пагинацией."""
        from src.crm.store import REPLY_CATEGORY_LABELS, STATUS_LABELS, VERIFY_LABELS
        from src.crm.vitality import vitality_payload

        try:
            limit = min(max(int(query.get("limit", "100")), 1), 1000)
            offset = max(int(query.get("offset", "0")), 0)
        except (TypeError, ValueError):
            limit, offset = 100, 0

        # Фильтры приходят списками через запятую: в интерфейсе их можно
        # выбирать по нескольку сразу (провинции, статусы).
        def multi(name):
            raw = query.get(name, "").strip()
            return [x for x in (part.strip() for part in raw.split(",")) if x] or None

        search = query.get("search", "")
        order_by = query.get("order_by", "score")
        descending = query.get("order", "desc").lower() != "asc"
        statuses = multi("status")
        provinces = multi("province")
        tiers = multi("tier")
        categories = multi("category")
        verified_only = query.get("verified_only", "1") not in ("0", "false", "no")

        # Живость: фильтр «только молчуны / только отвечающие»
        vitality = (query.get("vitality") or "").strip()
        vitality = vitality if vitality in ("replies", "silent") else None

        store = self.store()
        kwargs = dict(statuses=statuses, provinces=provinces, search=search, tiers=tiers,
                      reply_categories=categories, vitality=vitality)
        rows = store.list_sellers(
            verified_only=verified_only,
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
            **kwargs,
        )
        total = store.count_sellers(verified_only=verified_only, **kwargs)
        companies = store.same_company_counts()

        payload = []
        for row in rows:
            item = dict(row)
            # Сколько ещё витрин у той же компании — подсказка в интерфейсе
            item["same_company"] = companies.get(item.get("uscc") or "", 1)
            # Живость: готовый срез для колонки (числа честные, из базы)
            item["vitality"] = vitality_payload(
                int(item.get("reviews_seen") or 0), int(item.get("replies_seen") or 0)
            )
            payload.append(item)

        return {
            "rows": payload,
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_labels": STATUS_LABELS,
            "verify_labels": VERIFY_LABELS,
            "reply_labels": REPLY_CATEGORY_LABELS,
            "provinces": store.province_facets(),
        }

    def export_csv(self, query: dict) -> str:
        """Выгрузка продавцов в CSV с учётом активных фильтров."""
        import csv
        import io

        def multi(name):
            raw = query.get(name, "").strip()
            return [x for x in (part.strip() for part in raw.split(",")) if x] or None

        store = self.store()
        rows = store.list_sellers(
            verified_only=query.get("verified_only", "1") not in ("0", "false", "no"),
            statuses=multi("status"),
            provinces=multi("province"),
            tiers=multi("tier"),
            reply_categories=multi("category"),
            vitality=(query.get("vitality") or "").strip() or None,
            search=query.get("search", ""),
            order_by=query.get("order_by", "score"),
            descending=query.get("order", "desc").lower() != "asc",
            limit=1_000_000,
            offset=0,
        )

        columns = [
            ("seller_id", "seller_id"), ("shop_name", "Магазин"),
            ("company_name", "Компания"), ("uscc", "USCC"),
            ("province", "Провинция"), ("verified_by", "Достоверность"),
            ("orders", "Заказов"), ("reviews", "Отзывов"),
            ("months_on_ozon", "Месяцев на Ozon"), ("rating", "Рейтинг"),
            ("score", "Оценка"), ("status", "Статус"),
            ("reply_category", "Категория ответа"), ("reply_at", "Ответ получен"),
            ("handed_to_manager_at", "Передан менеджеру"),
            ("notes", "Заметка"),
            ("seller_profile_url", "Профиль продавца"),
            ("sample_product_url", "Товар"),
        ]
        # BOM и точка с запятой: файл открывают в Excel с русской локалью
        from src.crm.vitality import CAT_UNKNOWN, vitality_payload

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow([title for _, title in columns] + ["Живость"])
        for row in rows:
            v = vitality_payload(int(row["reviews_seen"] or 0), int(row["replies_seen"] or 0))
            if v["seen"] <= 0:
                vitality_text = ""
            elif v["category"] == CAT_UNKNOWN:
                vitality_text = "мало данных"
            else:
                vitality_text = f'{v["label"]} {v["replies"]}/{v["seen"]}'
            writer.writerow([row[key] for key, _ in columns] + [vitality_text])
        return "\ufeff" + buffer.getvalue()

    def detail(self, seller_id: str) -> dict:
        """Всё по одному продавцу для карточки."""
        store = self.store()
        row = store.get_seller(seller_id)
        if not row:
            return {}
        item = dict(row)
        companies = store.same_company_counts()
        item["same_company"] = companies.get(item.get("uscc") or "", 1)
        return item

    def summary(self) -> dict:
        """Сводка для шапки: сколько чего, когда собирали, когда бэкап."""
        return self.store().recent_stats()

    def set_note(self, seller_id: str, note: str) -> dict:
        return {"ok": self.store().set_note(seller_id, note)}

    def set_status(self, seller_id: str, status: str) -> dict:
        return {"ok": self.store().set_status(seller_id, status)}

    def set_category(self, seller_id: str, category: str) -> dict:
        try:
            return {"ok": self.store().set_reply_category(seller_id, category)}
        except ValueError as exc:
            return {"error": str(exc)}

    def handover(self, seller_id: str) -> dict:
        return {"ok": self.store().hand_to_manager(seller_id)}


# Задержка между запросами по умолчанию — как в харвестере
try:
    from src.crm.harvest import DEFAULT_DELAY
except Exception:                                            # noqa: BLE001
    DEFAULT_DELAY = 0.7

CRUD = SellersApi(DB_PATH)
# База WB — отдельный файл (решение владельца: данные маркетплейсов не
# смешиваются). Тот же SellerStore и та же статусная модель, свой файл:
# задаёшь путь — схема создаётся автоматически, миграций живой базе Ozon
# не требуется.
WB_DB_PATH = DATA_DIR / "sellers-wb.sqlite3"
CRUD_WB = SellersApi(WB_DB_PATH)
AUTH = load_credentials()

# Сессия дашборда. HTTP Basic не подходит: встроенные браузеры не показывают
# окно авторизации и отдают голый JSON «Требуется вход» — выглядит как «не
# открывается». Поэтому вход через обычную форму, а токен живёт в cookie.
SESSION_COOKIE = "ozon_crm_session"
SESSION_TOKEN = secrets.token_urlsafe(24)

# Замок перебора пароля: 6 неудач с одного IP подряд — вход с этого IP
# молчит 10 минут. Очередь старых неудач чистится по возрасту.
LOGIN_MAX_FAILS = 6
LOGIN_BLOCK_SECONDS = 600
LOGIN_FAILS: deque = deque()
LOGIN_FAILS_BY_IP: dict = {}
LOGIN_BLOCKS: dict = {}

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>Вход — Ozon CRM</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0b0e14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Ozon CRM">
<style>
  :root{--bg1:#0d1017;--bg2:#101c2e;--accent:#2f7cf6;--accent2:#22d3ee;
        --text:#e6e9ef;--dim:#8b93a1;--card:rgba(23,26,33,.72);--line:rgba(255,255,255,.08)}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--text);padding:20px;touch-action:manipulation;overscroll-behavior:none;
    background:
      radial-gradient(900px 480px at 15% -10%, rgba(47,124,246,.28), transparent 60%),
      radial-gradient(700px 420px at 90% 110%, rgba(34,211,238,.16), transparent 60%),
      linear-gradient(160deg, var(--bg1) 0%, var(--bg2) 100%)}
  .wrap{width:340px;max-width:100%;animation:rise .5s cubic-bezier(.22,1,.36,1)}
  @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  .logo{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:16px}
  .logo .mark{width:38px;height:38px;border-radius:12px;display:flex;align-items:center;
    justify-content:center;font-size:19px;font-weight:700;color:#fff;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    box-shadow:0 6px 22px rgba(47,124,246,.45)}
  .logo b{font-size:18px;letter-spacing:.2px}
  form{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:26px 24px 22px;backdrop-filter:blur(10px);
    box-shadow:0 24px 60px rgba(0,0,0,.45)}
  h1{margin:0 0 4px;font-size:19px;font-weight:600}
  p{margin:0 0 20px;color:var(--dim);font-size:13px}
  label{display:block;font-size:12px;color:var(--dim);margin-bottom:6px}
  input{width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--line);
    background:rgba(13,16,23,.65);color:var(--text);font:inherit;font-size:16px;
    transition:border-color .18s, box-shadow .18s}
  input:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(47,124,246,.22)}
  button{width:100%;margin-top:16px;padding:12px;border:none;border-radius:10px;
    background:linear-gradient(135deg,var(--accent),#4a8cf7);color:#fff;font:inherit;
    font-size:14px;font-weight:600;cursor:pointer;letter-spacing:.2px;
    transition:transform .12s, box-shadow .18s, filter .18s}
  button:hover{filter:brightness(1.1);box-shadow:0 10px 26px rgba(47,124,246,.4)}
  button:active{transform:translateY(1px)}
  .pw-wrap{position:relative}
  .pw-wrap input{padding-right:46px}
  .pw-eye{position:absolute;right:5px;top:50%;transform:translateY(-50%);
    width:36px;height:36px;border:none;background:transparent;color:var(--dim);
    cursor:pointer;display:flex;align-items:center;justify-content:center;
    border-radius:9px;padding:0;margin:0}
  .pw-eye:hover{background:rgba(255,255,255,.07);color:var(--text);filter:none;box-shadow:none}
  .pw-eye svg{width:20px;height:20px;pointer-events:none}
  .err{margin-top:12px;color:#f87171;font-size:13px;min-height:18px;text-align:center}
  @media (max-width:420px){.wrap{width:100%}}
</style></head><body>
<div class="wrap">
  <div class="logo"><span class="mark">O</span><b>Ozon CRM</b></div>
  <form method="POST" action="/api/login">
    <h1>Вход в дашборд</h1>
    <p>Введите пароль, чтобы продолжить</p>
    <label for="pw">Пароль</label>
    <div class="pw-wrap">
      <input type="password" id="pw" name="password" autofocus autocomplete="current-password" placeholder="пароль">
      <button type="button" class="pw-eye" id="pwEye" aria-label="Показать пароль" title="Показать пароль"></button>
    </div>
    <button type="submit">Войти</button>
    <div class="err">__ERROR__</div>
  </form>
</div>
<script>
(function(){
  var eye = document.getElementById("pwEye");
  var pw = document.getElementById("pw");
  var openSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var shutSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12A3 3 0 1 1 9.88 9.88"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  eye.innerHTML = shutSvg;
  eye.addEventListener("click", function(){
    var show = pw.type === "password";
    pw.type = show ? "text" : "password";
    eye.innerHTML = show ? openSvg : shutSvg;
    var label = show ? "Скрыть пароль" : "Показать пароль";
    eye.title = label;
    eye.setAttribute("aria-label", label);
    pw.focus();
  });
})();
</script>
</body></html>"""

# Раздел Ozon Global, откуда берутся китайские продавцы. Подставляется в форму
# как значение по умолчанию — это и есть «та самая ссылка».
CHINA_SECTION_URL = "https://www.ozon.ru/highlight/tovary-iz-kitaya-935133/"

# Замок на браузерную сессию. Все задачи, которые лезут в Chrome, делят один
# профиль и один порт отладки: если запустить две одновременно, они начнут
# мешать друг другу — переключать вкладки и закрывать их друг у друга. Раньше
# проверки не было вообще, и «Запустить парсинг» + «Собрать» спокойно работали
# вдвоём.
BROWSER_LOCK = threading.Lock()


class HarvestRunner:
    """Запуск сбора китайских продавцов прямо из дашборда.

    Отдельно от `Service`: тот управляет старым парсером категорий (обход
    страницы со скроллом), а здесь — обход раздела Ozon Global по страницам с
    подтверждением страны по USCC. Это разные вещи, и смешивать их не нужно.

    Работает в фоновом потоке, прогресс отдаёт через общую шину событий, чтобы
    интерфейс рисовал его теми же компонентами, что и лог парсера.
    """

    def __init__(self):
        self.state = {
            "running": False,
            "url": "",
            "pages": 0,
            "phase": "",
            "progress": 0,
            "error": "",
            "stats": {},
            "started_at": None,
            "finished_at": None,
        }
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._holds_browser = False

    def status(self) -> dict:
        return dict(self.state)

    def start(self, url: str, pages: int, delay: Optional[float] = None) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Сбор уже идёт")

            url = (url or "").strip() or CHINA_SECTION_URL
            if "ozon.ru" not in url:
                raise ValueError("Укажите ссылку на раздел Ozon")
            # Лимит 500 держим вровень с полем «страниц» в dashboard/web/index.html
            # (max="500"); сам харвест страницы не ограничивает, обход останавливается
            # сам, когда Ozon перестаёт отдавать новые товары.
            pages = max(1, min(int(pages or 10), 500))

            # Занимаем браузер, пока сбор не закончится
            if NIGHT.running():
                raise ValueError("Идёт ночной сбор — остановите его или дождитесь утра")
            if not BROWSER_LOCK.acquire(blocking=False):
                raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
            self._holds_browser = True

            self._stop.clear()
            self._delay = delay
            self.state.update({
                "running": True, "url": url, "pages": pages,
                "phase": "Запускаю браузер", "progress": 0,
                "error": "", "stats": {},
                "started_at": time.time(), "finished_at": None,
            })

        self._thread = threading.Thread(
            target=self._run, args=(url, pages, getattr(self, "_delay", None)), daemon=True
        )
        self._thread.start()

    def _release_browser(self) -> None:
        if getattr(self, "_holds_browser", False):
            try:
                BROWSER_LOCK.release()
            except RuntimeError:
                pass
            self._holds_browser = False

    def stop(self) -> None:
        """Просит сбор остановиться. Реально работает: харвестер проверяет
        флаг в каждом цикле и выходит на ближайшей итерации."""
        self._stop.set()
        self.state["phase"] = "Останавливаю…"
        HUB.publish({"type": "log", "line": "Запрошена остановка сбора…"})
        HUB.publish({"type": "harvest", **self.state})

    def _release_browser(self) -> None:
        if self._holds_browser:
            try:
                BROWSER_LOCK.release()
            except RuntimeError:
                pass
            self._holds_browser = False

    def _run(self, url: str, pages: int, delay: Optional[float] = None) -> None:
        def log(message: str) -> None:
            HUB.publish({"type": "log", "line": message})

        def progress(done: int, total: int, phase: str) -> None:
            # Три фазы разной длины: сбор товаров, определение продавцов,
            # проверка реквизитов. В одну шкалу их сводим с весами.
            weights = {"Собираю товары": (0, 40), "Определяю продавцов": (40, 35),
                       "Проверяю реквизиты": (75, 25)}
            start, span = weights.get(phase, (0, 0))
            value = start + (span * done / total if total else 0)
            self.state["phase"] = phase
            self.state["progress"] = round(min(value, 100), 1)
            HUB.publish({"type": "harvest", **self.state})

        try:
            from src.crm.harvest import ChinaSellerHarvester, OzonPageClient
            from src.crm.store import SellerStore
            from src.utils.browser import create_browser_manager

            log(f"Раздел: {url}")
            log(f"Страниц: {pages} (по 8 товаров)")

            manager = create_browser_manager(headless=True)
            driver = manager.create_driver()
            try:
                client = OzonPageClient(
                    driver, delay=delay if delay is not None else DEFAULT_DELAY
                )
                client.ensure_ready()
                harvester = ChinaSellerHarvester(
                    client=client,
                    store=SellerStore(DB_PATH),
                    on_progress=log,
                    progress_cb=progress,
                    stop_event=self._stop,
                )
                stats = harvester.run(url, pages=pages)
            finally:
                try:
                    manager.close()
                except Exception:                            # noqa: BLE001
                    pass

            self._release_browser()
            self.state.update({
                "running": False,
                "phase": "Остановлено" if stats.cancelled else "Готово",
                "progress": 100,
                "stats": {
                    "pages": stats.pages, "products": stats.products,
                    "sellers": stats.sellers_found,
                    "uscc": stats.sellers_verified_uscc,
                    "warehouse": stats.sellers_verified_warehouse,
                    "rejected": stats.sellers_rejected,
                    "requests": stats.requests,
                    "cancelled": bool(stats.cancelled),
                    "errors": list(stats.errors)[:20],
                },
                "finished_at": time.time(),
            })
            if stats.cancelled:
                log(
                    f"Сбор остановлен по запросу: страниц {stats.pages}, "
                    f"товаров {stats.products}, продавцов проверено {stats.sellers_found}"
                )
            else:
                log(
                    f"Сбор завершён: страниц {stats.pages}, товаров {stats.products}, "
                    f"продавцов {stats.sellers_found} "
                    f"(по USCC {stats.sellers_verified_uscc}, по складу {stats.sellers_verified_warehouse})"
                )
            HUB.publish({"type": "harvest", **self.state})
            HUB.publish({"type": "harvest_done", "stats": self.state["stats"]})
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Сбор не удался")
            self._release_browser()
            self.state.update({
                "running": False, "phase": "Ошибка", "error": str(exc)[:400],
                "finished_at": time.time(),
            })
            log(f"Ошибка сбора: {exc}")
            HUB.publish({"type": "harvest", **self.state})
            HUB.publish({"type": "harvest_done", "stats": {}, "error": str(exc)[:400]})


HARVEST = HarvestRunner()


class NightHarvest:
    """Ночной обход раздела: длинный сбор чанками, запускается кнопкой.

    Живёт отдельным процессом (tools/night_harvest.py), а не потоком внутри
    дашборда: обход идёт часами, и перезапуск дашборда не должен его убивать.
    Никакого расписания — только ручной запуск и ручная остановка; прогресс
    процесс хранит сам, поэтому после сбоя продолжает с той же страницы.
    """

    STATE_FILE = DATA_DIR / "night_harvest_state.json"
    STOP_FILE = DATA_DIR / "night_harvest.stop"
    LOCK_FILE = DATA_DIR / "night_harvest.lock"
    LOG_DIR = REPO_ROOT / "logs"
    SCRIPT = REPO_ROOT / "tools" / "night_harvest.py"

    def _pid(self) -> int:
        try:
            return int(self.LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def running(self) -> bool:
        """Идёт ли ночной сбор. Смотрим по pid: процесс переживает дашборд."""
        pid = self._pid()
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _log_path(self) -> Path:
        return self.LOG_DIR / f"night_harvest_{datetime.now().strftime('%Y-%m-%d')}.log"

    def _tail(self, lines: int = 30) -> list:
        """Хвост сегодняшнего лога — его показываем в дашборде."""
        path = self._log_path()
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()][-lines:]

    def status(self) -> dict:
        state = {}
        try:
            state = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        totals = state.get("totals") or {}
        log_lines = self._tail(120)
        # В логе каждая строка приходит дважды (logger + stdout): подряд идущие
        # дубли схлопываем, чтобы фильтры подробностей не показывали одно дважды
        log_lines = [line for i, line in enumerate(log_lines)
                     if i == 0 or line != log_lines[i - 1]]
        # «Сбор жив?» — вопрос, который возникает сразу, если лог подолгу не
        # меняется: показываем время последней записи и страницу, на которой
        # сборщик находится прямо сейчас.
        log_updated = ""
        try:
            log_updated = datetime.fromtimestamp(
                self._log_path().stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            log_updated = ""
        current_page = 0
        for line in reversed(log_lines):
            found = re.search(r"Страница (\d+):", line)
            if found:
                current_page = int(found.group(1))
                break

        # --- живой разбор: что происходит внутри чанка прямо сейчас ----------
        # Окно парсинга много больше показываемого хвоста: между «Страница N:»
        # и концом файла бывают десятки строк прогресса по товарам/продавцам,
        # из-за чего «Сейчас на странице» показывал прочерк.
        live = self._parse_live(self._tail(400))
        if live.get("page"):
            current_page = live["page"]

        return {
            "running": self.running(),
            "stop_requested": self.STOP_FILE.exists(),
            "next_page": int(state.get("next_page") or 1),
            "current_page": current_page,
            "chunks_done": int(state.get("chunks_done") or 0),
            "updated_at": state.get("updated_at") or "",
            # Чем кончился последний чанк и сколько сбоев подряд: по этому
            # «Обновить» при незапущенном сборе показывает не заглушки, а статус
            "last_result": state.get("last_result") or "",
            "failures": int(state.get("failures") or 0),
            "log_updated_at": log_updated,
            "days": {
                "products": int(totals.get("products") or 0),
                "sellers": int(totals.get("sellers") or 0),
                "uscc": int(totals.get("uscc") or 0),
                "errors": int(totals.get("errors") or 0),
            },
            "live": live,
            "log": log_lines,
        }

    # Порядок важен: фаза определяется САМОЙ СВЕЖЕЙ строкой лога, которую
    # узнаёт один из шаблонов. Строки в логе дублируются (logger + stdout) —
    # на «самую свежую» это не влияет.
    _LIVE_PATTERNS = (
        ("проверяю реквизиты", re.compile(r"Проверено продавцов: (\d+)/(\d+)")),
        ("определяю продавцов", re.compile(r"Обработано товаров: (\d+)/(\d+), продавцов (\d+)")),
        ("записываю в базу", re.compile(r"В базу: добавлено (\d+), свежих пропущено (\d+)")),
        ("собираю товары", re.compile(r"Страница (\d+):")),
        ("пауза между проходами", re.compile(r"(за ночь: чанков|итог: страниц|Готово\. страниц)")),
    )

    @classmethod
    def _parse_live(cls, lines: List[str]) -> dict:
        """Фаза и числа текущего прохода из хвоста лога ночного сбора.

        Числа собираются по всем шаблонам независимо (какое значение последнее
        в логе, такое и актуально), а фаза — по самой свежей узнанной строке.
        """
        live = {
            "phase": "", "page": 0, "chunk_start": 0, "chunk_end": 0,
            "items_done": 0, "items_total": 0, "sellers_found": 0,
            "checked_done": 0, "checked_total": 0, "added": 0, "skipped": 0,
        }
        fresh = list(reversed([line for line in lines if line.strip()]))
        seen = set()
        for line in fresh:
            for phase, pattern in cls._LIVE_PATTERNS:
                match = pattern.search(line)
                if not match or phase in seen:
                    continue
                # Каждое значение — из САМОЙ СВЕЖЕЙ своей строки: идём от
                # конца лога, первое совпадение шаблона и фиксируем
                seen.add(phase)
                if not live["phase"]:
                    live["phase"] = phase
                if phase == "проверяю реквизиты":
                    live["checked_done"], live["checked_total"] = int(match.group(1)), int(match.group(2))
                elif phase == "определяю продавцов":
                    live["items_done"], live["items_total"] = int(match.group(1)), int(match.group(2))
                    live["sellers_found"] = int(match.group(3))
                elif phase == "записываю в базу":
                    live["added"], live["skipped"] = int(match.group(1)), int(match.group(2))
                elif phase == "собираю товары":
                    live["page"] = int(match.group(1))
                break
        # Диапазон текущего прохода ищем по всему окну: он печатается один раз
        # в начале чанка и нужен для процента полосы
        for line in fresh:
            found = re.search(r"чанк: страницы (\d+)–(\d+)", line)
            if found:
                live["chunk_start"], live["chunk_end"] = int(found.group(1)), int(found.group(2))
                break
        return live

    def start(self, chunk_pages: int, until: str, pause: float, refresh_days: int,
              url: str = "") -> None:
        if self.running():
            raise ValueError("Ночной сбор уже идёт")
        if HARVEST.status().get("running"):
            raise ValueError("Сейчас идёт обычный сбор — дождитесь его окончания")
        if SEND.status().get("running"):
            raise ValueError("Идёт рассылка — ночной сбор подождёт её")
        if not self.SCRIPT.exists():
            raise ValueError(f"Нет скрипта ночного сбора: {self.SCRIPT}")

        # Старый стоп-кран от прошлого запуска иначе остановит новый сразу
        if self.STOP_FILE.exists():
            self.STOP_FILE.unlink()

        command = [
            sys.executable, str(self.SCRIPT),
            "--until", until,
            "--chunk-pages", str(chunk_pages),
            "--pause", str(pause),
            "--refresh-days", str(refresh_days),
        ]
        # Ссылку передаём только заданную: пусто — раздел по умолчанию скрипта.
        # Так ночной сбор может идти по любому разделу или поиску, не только
        # по «Товарям из Китая».
        if url:
            command += ["--url", url]
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with self._log_path().open("a", encoding="utf-8") as handle:
            subprocess.Popen(
                command, cwd=str(REPO_ROOT), stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        HUB.publish({"type": "log", "line": f"Ночной сбор запущен: {chunk_pages} страниц за проход, до {until}"})

    def stop(self) -> None:
        """Просит остановиться: текущий чанк доиграет, новые не начнутся."""
        self.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STOP_FILE.write_text("стоп\n", encoding="utf-8")
        HUB.publish({"type": "log", "line": "Ночной сбор: запрошена остановка (докрутит текущий чанк)"})


NIGHT = NightHarvest()


# --- Сохраняемые настройки сборов -------------------------------------------
# Обычный и ночной сбор хранят свои ссылки и параметры в data/: файл не в git,
# деплой data/ не трогает — на каждой машине свои настройки и они переживают
# перезапуск дашборда. Пустая ссылка = раздел по умолчанию («Товары из Китая»).
HARVEST_SETTINGS_FILE = DATA_DIR / "harvest_settings.json"

HARVEST_SETTINGS_DEFAULTS = {
    "harvest": {"url": "", "pages": 50, "delay": 0.7},
    "night": {"url": "", "chunk_pages": 10, "until": "08:00", "pause": 180, "refresh_days": 7},
}


def load_harvest_settings() -> dict:
    """Сохранённые настройки, дополненные значениями по умолчанию."""
    import copy

    settings = copy.deepcopy(HARVEST_SETTINGS_DEFAULTS)
    try:
        stored = json.loads(HARVEST_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    for section in settings:
        if not isinstance(stored.get(section), dict):
            continue
        for key in settings[section]:
            if key in stored[section]:
                settings[section][key] = stored[section][key]
    return settings


def save_harvest_settings(data: dict) -> dict:
    """Валидирует и сохраняет присланные секции (harvest и/или night).

    Валидация до записи: кривое «работать до» не должно доехать до запуска
    ночного сбора и уронить его на первом же чанке.
    """
    settings = load_harvest_settings()
    for section in ("harvest", "night"):
        incoming = (data or {}).get(section)
        if not isinstance(incoming, dict):
            continue
        target = settings[section]
        for key, default in target.items():
            if key not in incoming:
                continue
            value = incoming[key]
            try:
                if isinstance(default, bool):
                    target[key] = bool(value)
                elif isinstance(default, int):
                    target[key] = int(value)
                elif isinstance(default, float):
                    target[key] = float(value)
                else:
                    target[key] = str(value).strip()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Неверное значение {section}.{key}: {value!r}") from exc

    harvest, night = settings["harvest"], settings["night"]
    if harvest["url"] and "ozon.ru" not in harvest["url"]:
        raise ValueError("Ссылка обычного сбора должна вести на ozon.ru")
    harvest["pages"] = min(max(harvest["pages"], 1), 500)
    harvest["delay"] = min(max(harvest["delay"], 0.0), 10.0)

    if night["url"] and "ozon.ru" not in night["url"]:
        raise ValueError("Ссылка ночного сбора должна вести на ozon.ru")
    night["chunk_pages"] = min(max(night["chunk_pages"], 1), 100)
    night["pause"] = min(max(night["pause"], 5), 3600)
    night["refresh_days"] = min(max(night["refresh_days"], 0), 365)
    if night["until"] and not re.match(r"^\d{1,2}:\d{2}$", night["until"]):
        raise ValueError(f"«Работать до» должно быть ЧЧ:ММ, а не «{night['until']}»")

    HARVEST_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HARVEST_SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def resume_night_after_restart() -> Optional[str]:
    """Продолжает ночной сбор, убитый перезапуском дашборда.

    Ночной сбор — дочерний процесс дашборда и живёт в его systemd-cgroup:
    рестарт службы убивает его, причём двумя способами. SIGKILL (KillMode
    по умолчанию) убивает мгновенно — lock остаётся, стейт свежий. SIGTERM
    скрипт обрабатывает штатно: доигрывает шаг, снимает lock, пишет «финиш»
    — и внешне это неотличимо от планового завершения. Различаем так:

    * процесс жив — не мешаем;
    * стоп-кран стоит — пользователь просил остановить, не возобновляем;
    * в хвосте лога есть «время вышло» / «достигнут предел» — сбор закончился
      сам по своей остановке времени, не возобновляем;
    * прогресс (state.updated_at) свежее трёх часов и завершения не было —
      значит убили посреди работы: продолжаем с сохранёнными настройками,
      скрипт начинает с последней записанной в стейт страницы.
    """
    if NIGHT.running():
        return None
    if NIGHT.STOP_FILE.exists():
        # Пользователь просил остановить — уважаем стоп-кран
        NIGHT.LOCK_FILE.unlink(missing_ok=True)
        return None

    try:
        updated = NIGHT.status().get("updated_at") or ""
        fresh = updated and (
            datetime.now() - datetime.fromisoformat(updated)
        ).total_seconds() < 3 * 3600
    except (ValueError, OSError):
        fresh = False
    if not fresh:
        return None

    # Штатное завершение видно по хвосту лога: без него любой перезапуск
    # дашборда возобновлял бы сбор до бесконечности
    tail = " ".join(NIGHT._tail(8))
    finished_naturally = ("время вышло" in tail) or ("достигнут предел" in tail)
    if "финиш:" in tail and finished_naturally:
        return None

    # Мёртвый lock не должен блокировать новый запуск скрипта
    NIGHT.LOCK_FILE.unlink(missing_ok=True)
    night = load_harvest_settings()["night"]
    NIGHT.start(
        chunk_pages=night["chunk_pages"], until=night["until"], pause=night["pause"],
        refresh_days=night["refresh_days"], url=night["url"],
    )
    return (
        f"Ночной сбор пережил перезапуск дашборда — продолжен с его настроек "
        f"(до {night['until']}, порции по {night['chunk_pages']} страниц)"
    )


class InboxAuto:
    """Автопроверка входящих чатов по расписанию: раз в 30 минут.

    Работает в фоновом потоке дашборда. Браузер общий: если сбор, рассылка
    или ответ продавцу его заняли — этот цикл просто пропускается и проверка
    повторится в следующий раз. Результат — в лог дашборда и в Telegram
    (если настроен chat_id), новые ответы сразу получают статус «ответил».
    """

    INTERVAL = 1800        # 30 минут
    FIRST_DELAY = 120      # первая проверка через 2 минуты после старта

    def __init__(self):
        self.state = {
            "last_run": "",
            "last_result": "",
            "found": 0,
            "interval_min": self.INTERVAL // 60,
        }
        self._busy = threading.Lock()

    def status(self) -> dict:
        return dict(self.state)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="inbox-auto").start()

    def _loop(self) -> None:
        time.sleep(self.FIRST_DELAY)
        while True:
            self._run_once()
            time.sleep(self.INTERVAL)

    def _run_once(self) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            if not BROWSER_LOCK.acquire(blocking=False):
                self.state["last_result"] = "пропущено — браузер занят"
                return
            try:
                from src.crm.inbox import InboxChecker

                result = InboxChecker(CRUD.store(), data_dir=SEND.DATA_DIR).check()
                self.state.update({
                    "last_run": datetime.now().isoformat(timespec="seconds"),
                    "last_result": (
                        f"чатов {result['chats']}, непрочитанных {result['unread']}, "
                        f"пометили {len(result['marked'])}"
                    ),
                    "found": len(result["marked"]),
                })
                HUB.log(
                    "Автопроверка чатов: "
                    + self.state["last_result"]
                    + (f", не совпали с базой: {', '.join(result['unmatched'])}"
                       if result["unmatched"] else "")
                )
                from src.notify import telegram as tg

                for marked in result["marked"]:
                    tg.send(f"💬 Ответил: {marked['shop']}\n{marked['text'][:400]}")
            except Exception as exc:                  # noqa: BLE001
                self.state.update({
                    "last_run": datetime.now().isoformat(timespec="seconds"),
                    "last_result": f"ошибка: {str(exc)[:160]}",
                })
            finally:
                BROWSER_LOCK.release()
        finally:
            self._busy.release()


INBOX_AUTO = InboxAuto()


class SendRunner:
    """Отправка сообщений из дашборда.

    Работает с ОБЩЕЙ базой продавцов (`data/sellers.sqlite3`), а не с
    отдельной очередью в `outreach_data/` — раньше отправка смотрела в файл,
    которого не существует, и отправлять было физически некому.
    """

    DATA_DIR = REPO_ROOT / "outreach_data"
    TEMPLATES_DIR = DATA_DIR / "templates"
    CONFIG_FILE = DATA_DIR / "sender.json"

    def __init__(self):
        self.state = {
            "running": False, "phase": "", "sent": 0, "failed": 0,
            "error": "", "dry_run": True, "last_event": {},
            "started_at": None, "finished_at": None,
        }
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._holds_browser = False

    # --- настройки -------------------------------------------------------- #
    def config(self):
        from src.outreach.sender import SenderConfig
        return SenderConfig.load(self.CONFIG_FILE)

    def settings(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self.config())

    def save_settings(self, data: dict) -> dict:
        import dataclasses
        config = self.config()
        for key, value in (data or {}).items():
            if not hasattr(config, key):
                continue
            current = getattr(config, key)
            try:
                if isinstance(current, bool):
                    setattr(config, key, bool(value))
                elif isinstance(current, int):
                    setattr(config, key, int(value))
                elif isinstance(current, float):
                    setattr(config, key, float(value))
                else:
                    setattr(config, key, str(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Неверное значение для {key}: {value}") from exc
        config.save(self.CONFIG_FILE)
        return dataclasses.asdict(config)

    def template(self):
        from src.outreach.templates import TemplateError, list_templates, load_template
        paths = list_templates(self.TEMPLATES_DIR)
        if not paths:
            return None, "в outreach_data/templates нет ни одного шаблона"
        try:
            return load_template(paths[0]), ""
        except TemplateError as exc:
            return None, str(exc).split("\n")[0]

    # --- шаблон сообщения -------------------------------------------------- #
    MAIN_TEMPLATE = "main.txt"

    def template_path(self) -> Path:
        return self.TEMPLATES_DIR / self.MAIN_TEMPLATE

    def read_template(self) -> dict:
        """Текущий текст сообщения. Если файла нет — отдаём заготовку-образец."""
        path = self.template_path()
        example = self.TEMPLATES_DIR / "example.txt.example"
        body, source = "", "empty"
        if path.exists():
            body, source = path.read_text(encoding="utf-8"), "file"
        elif example.exists():
            body, source = example.read_text(encoding="utf-8"), "example"

        from src.outreach.templates import ALLOWED_FIELDS, _plural  # noqa: F401
        from src.outreach.templates import PLACEHOLDER_MARKER, OPTOUT_MARKERS

        lowered = body.lower()
        return {
            "name": path.stem,
            "body": body,
            "source": source,
            "path": str(path.name),
            "variables": sorted(ALLOWED_FIELDS),
            "is_placeholder": PLACEHOLDER_MARKER in body,
            "has_optout": any(marker in lowered for marker in OPTOUT_MARKERS),
        }

    def save_template(self, body: str) -> dict:
        """Проверяет и сохраняет текст сообщения.

        Проверяем до записи: без строки отказа от рассылки шаблон отклоняется,
        и лучше узнать об этом здесь, чем при запуске отправки.
        """
        from src.outreach.templates import MessageTemplate, TemplateError

        self.TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        candidate = MessageTemplate(name=self.MAIN_TEMPLATE[:-4], body=body or "")
        try:
            candidate.validate()
        except TemplateError as exc:
            return {"ok": False, "error": str(exc)}

        self.template_path().write_text(body, encoding="utf-8")
        result = self.read_template()
        result["ok"] = True
        result["warning"] = (
            "В тексте осталась метка-заготовка — реальная отправка будет заблокирована"
            if candidate.is_placeholder() else ""
        )
        return result

    def preview_message(self, seller_id: Optional[str] = None) -> dict:
        """Показывает готовый текст на данных реального продавца."""
        from src.outreach.templates import TemplateError

        template, error = self.template()
        if template is None:
            return {"error": error or "шаблон не загружен"}

        store = CRUD.store()
        if seller_id:
            row = store.get_seller(seller_id)
        else:
            rows = store.list_sellers(statuses=["new"], limit=1)
            row = rows[0] if rows else None
        if row is None:
            return {"error": "В очереди нет продавцов для примера"}

        try:
            return {
                "seller_id": row["seller_id"],
                "shop_name": row["shop_name"] or row["seller_id"],
                "orders": row["orders"],
                "text": template.render(dict(row)),
            }
        except TemplateError as exc:
            return {"error": str(exc)}

    # --- проверка готовности ---------------------------------------------- #
    def preflight(self) -> dict:
        from src.outreach.sender import preflight
        template, error = self.template()
        result = preflight(self.config(), template, self.DATA_DIR, store=CRUD.store())
        if error:
            result["template_error"] = error
        result["template_name"] = getattr(template, "name", "")
        return result

    # --- запуск ------------------------------------------------------------ #
    def start(self, dry_run: Optional[bool] = None, limit: Optional[int] = None) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Отправка уже идёт")

            config = self.config()
            if dry_run is not None:
                config.dry_run = bool(dry_run)
            template, error = self.template()
            if template is None:
                raise ValueError(error or "не найден шаблон сообщения")

            from src.outreach.templates import TemplateError
            try:
                template.validate()
            except TemplateError as exc:
                raise ValueError(str(exc).split("\n")[0]) from exc

            # Заготовку не отправляем даже по прямой команде — проверка живёт
            # здесь, а не только в разборе аргументов CLI
            if template.is_placeholder() and not config.dry_run:
                raise ValueError(
                    "В шаблоне осталась метка «ЗАМЕНИТЕ ЭТОТ ТЕКСТ» — "
                    "напишите свой текст или включите предпросмотр"
                )

            if not config.dry_run:
                report = self.preflight()
                if not report["ready"]:
                    titles = [c["title"] for c in report["checks"]
                              if c["blocking"] and not c["ok"]]
                    raise ValueError("Не готово к отправке: " + ", ".join(titles))
                if not BROWSER_LOCK.acquire(blocking=False):
                    raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
                self._holds_browser = True

            self._stop.clear()
            self.state.update({
                "running": True, "phase": "Запуск", "sent": 0, "failed": 0,
                "error": "", "dry_run": config.dry_run, "last_event": {},
                # Причину остановки прошлого прогона чистим обязательно: иначе
                # новый запуск с первой секунды показывал бы «остановлено вручную»
                "stopped_reason": "",
                "limit": limit, "started_at": time.time(), "finished_at": None,
            })

        self._thread = threading.Thread(
            target=self._run, args=(config, template, limit), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Просит отправку остановиться. Реально работает: цикл проверяет флаг
        каждую секунду, а не раз в пять минут."""
        self._stop.set()
        self.state["phase"] = "Останавливаю…"
        HUB.publish({"type": "log", "line": "Запрошена остановка отправки…"})
        HUB.publish({"type": "send", **self.state})

    def _release_browser(self) -> None:
        if self._holds_browser:
            try:
                BROWSER_LOCK.release()
            except RuntimeError:
                pass
            self._holds_browser = False

    def _run(self, config, template, limit: Optional[int] = None) -> None:
        from src.outreach.sender import run_queue

        def log(message: str) -> None:
            HUB.publish({"type": "log", "line": message})

        def event(payload: dict) -> None:
            kind = payload.get("kind", "")
            if kind == "sent":
                self.state["sent"] = payload.get("total", self.state["sent"])
                self.state["phase"] = f"Отправлено: {payload.get('shop', '')}"
            elif kind == "failed":
                self.state["failed"] += 1
                self.state["phase"] = f"Ошибка: {payload.get('shop', '')}"
            elif kind == "skipped":
                self.state["skipped"] = self.state.get("skipped", 0) + 1
                self.state["phase"] = f"Пропущен: {payload.get('shop', '')}"
            elif kind == "sending":
                self.state["phase"] = f"Пишу: {payload.get('shop', '')}"
            elif kind == "waiting":
                self.state["phase"] = f"Пауза: {payload.get('reason', '')}"
            self.state["last_event"] = payload
            HUB.publish({"type": "send", **self.state})

        try:
            report = run_queue(
                queue=CRUD.store(),
                template=template,
                config=config,
                data_dir=self.DATA_DIR,
                on_progress=log,
                stop_event=self._stop,
                on_event=event,
                limit=limit,
            )
            self._release_browser()
            self.state.update({
                "running": False, "phase": "Готово",
                "sent": report.get("sent", self.state["sent"]),
                "failed": report.get("failed", self.state["failed"]),
                "skipped": report.get("skipped", self.state.get("skipped", 0)),
                "stopped_reason": report.get("stopped_reason", ""),
                "finished_at": time.time(),
            })
            log(f"Отправка завершена: отправлено {self.state['sent']}, ошибок {self.state['failed']}"
                + (f", пропущено {self.state['skipped']}" if self.state.get("skipped") else "")
                + (f" ({self.state['stopped_reason']})" if self.state.get("stopped_reason") else ""))
            from src.notify import telegram as tg

            tg.send(
                f"📨 Отправка завершена: отправлено {self.state['sent']}"
                f", ошибок {self.state['failed']}"
                + (f", пропущено {self.state['skipped']}" if self.state.get("skipped") else "")
                + (f"\n{self.state['stopped_reason']}" if self.state.get("stopped_reason") else "")
            )
            HUB.publish({"type": "send", **self.state})
            HUB.publish({"type": "send_done", "stats": self.state})
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Отправка не удалась")
            self._release_browser()
            self.state.update({
                "running": False, "phase": "Ошибка", "error": str(exc)[:400],
                "finished_at": time.time(),
            })
            log(f"Ошибка отправки: {exc}")
            HUB.publish({"type": "send", **self.state})
            HUB.publish({"type": "send_done", "stats": self.state, "error": str(exc)[:400]})

    def status(self) -> dict:
        return dict(self.state)


SEND = SendRunner()


# --------------------------------------------------------------------------- #
#  Шина событий: лог и статус уходят подписчикам SSE
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=500)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=3000)
        with self._lock:
            self._subs.add(q)
        for item in list(self.history):          # отдаём историю лога новому клиенту
            try:
                q.put_nowait(item)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def log(self, line: str, level: str = "INFO") -> None:
        """Публикует строку в лог. Удобно передавать как колбэк прогресса."""
        self.publish({"type": "log", "level": level, "ts": time.time(), "line": line})

    def publish(self, event: dict) -> None:
        if event.get("type") == "log":
            self.history.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


HUB = Hub()

# Супервайзер воркеров сбора (Ozon / WB — отдельные процессы). Должен
# создаваться после HUB: события воркеров уходят в SSE через его шину.
from workers import WorkerSupervisor                    # noqa: E402

WORKERS = WorkerSupervisor(REPO_ROOT, publish=HUB.publish)


# --------------------------------------------------------------------------- #
#  Оценка прогресса по строкам лога
# --------------------------------------------------------------------------- #
class Progress:
    """Парсер не сообщает прогресс напрямую, поэтому выводим его из лога."""

    def __init__(self):
        self.phase = "Ожидание"
        self.value = 0
        self.reset()

    def reset(self) -> None:
        self.phase = "Ожидание"
        self.value = 0
        self.products_total = 0
        self.products_done = 0
        self._last_emit = 0.0

    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.7:
            return
        self._last_emit = now
        HUB.publish({"type": "progress", "phase": self.phase, "value": round(self.value, 1)})

    def feed(self, line: str) -> None:
        m = re.search(r"Скролл \d+: \+\d+, всего (\d+)/(\d+)", line)
        if m:
            cur, total = int(m.group(1)), max(int(m.group(2)), 1)
            self.phase = "Сбор ссылок на товары"
            self.value = 5 + min(cur / total, 1.0) * 25
            return self._emit()

        m = re.search(r"Начало парсинга (\d+) товаров", line)
        if m:
            self.products_total = int(m.group(1))
            self.products_done = 0
            self.phase = "Парсинг товаров"
            self.value = 35
            return self._emit(force=True)

        if "обработан успешно" in line and "Товар" in line:
            self.products_done += 1
            if self.products_total:
                share = min(self.products_done / self.products_total, 1.0)
                self.value = 35 + share * 28
            return self._emit()

        if "Фильтр CHINA_ONLY" in line:
            self.phase = "Фильтр: только китайские продавцы"
            self.value = 66
            return self._emit(force=True)

        if "Начинаем парсинг" in line and "продавцов" in line:
            self.phase = "Парсинг продавцов"
            self.value = 74
            return self._emit(force=True)

        if "Продавец" in line and "обработан успешно" in line:
            self.value = min(self.value + 1.5, 95)
            return self._emit()

        if "Excel файл сохранен" in line:
            self.phase = "Экспорт отчёта"
            self.value = 98
            return self._emit(force=True)

        if "Не удалось собрать ссылки товаров" in line:
            self.phase = "Не удалось собрать ссылки"
            return self._emit(force=True)


PROGRESS = Progress()


class HubLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        HUB.publish({
            "type": "log",
            "level": record.levelname,
            "line": line,
            "ts": time.time(),
        })
        PROGRESS.feed(line)


# --------------------------------------------------------------------------- #
#  Сервис: обёртка над AppManager
# --------------------------------------------------------------------------- #
class Service:
    def __init__(self):
        self.manager = None
        self._manager_error: str = ""
        self._watch_thread = None
        self._lock = threading.Lock()
        self._holds_browser = False
        self.state = {
            "running": False,
            "phase": "Ожидание",
            "progress": 0,
            "error": "",
            "category_url": "",
            "max_products": 50,
            "china_only": True,
            "started_at": None,
            "finished_at": None,
            "parser_ready": None,     # None = ещё не проверяли
            "parser_error": "",
        }
        self._results_cache = {"products": [], "stats": {}}

    # --- доступ к парсеру -------------------------------------------------- #
    def get_manager(self):
        if self.manager is not None:
            return self.manager
        if self._manager_error:
            raise RuntimeError(self._manager_error)
        try:
            from src.config.settings import Settings
            from src.core.app_manager import AppManager
            self.manager = AppManager(Settings())
            self.state["parser_ready"] = True
        except Exception as exc:                      # noqa: BLE001
            self._manager_error = (
                f"Парсер недоступен: {exc}. "
                "Установите зависимости: pip3 install -r requirements.txt"
            )
            self.state["parser_ready"] = False
            self.state["parser_error"] = self._manager_error
            logging.getLogger(__name__).error(self._manager_error)
            raise RuntimeError(self._manager_error)
        return self.manager

    def check_parser(self) -> None:
        try:
            self.get_manager()
        except RuntimeError:
            pass

    # --- запуск / остановка ------------------------------------------------ #
    @staticmethod
    def ensure_global(url: str) -> str:
        """Добавляет фильтр «товары из-за рубежа», если его нет в ссылке."""
        if "ozon.ru" in url and "from_global" not in url:
            url += ("&" if "?" in url else "?") + "from_global=true"
        return url

    def start(self, url: str, max_products: int, china_only: bool) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Парсинг уже запущен")
            if not url or "ozon.ru" not in url:
                raise ValueError("Укажите ссылку на категорию или поиск Ozon")

            # Тот же замок, что у сбора: браузер один на всех
            if not BROWSER_LOCK.acquire(blocking=False):
                raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
            self._holds_browser = True

            manager = self.get_manager()
            manager.settings.MAX_PRODUCTS = max(1, int(max_products))
            manager.settings.CHINA_ONLY = bool(china_only)

            final_url = self.ensure_global(url)
            # ВАЖНО: кэш прошлого прогона НЕ обнуляем. Раньше здесь стояло
            # `self._results_cache = {"products": [], "stats": {}}`, и при запуске
            # нового парса таблица пропадала у пользователя. Теперь предыдущие
            # результаты остаются на экране, пока идёт новый обход, а по его
            # завершении заменяются свежими.
            PROGRESS.reset()
            PROGRESS.phase = "Запуск Chrome"
            PROGRESS.value = 3

            ok = manager.start_parsing(final_url, EXPORT_FIELDS)
            if not ok:
                self._release_browser()
                raise ValueError("Парсер уже занят другим заданием")

            self.state.update({
                "running": True,
                "phase": "Запуск",
                "progress": 3,
                "error": "",
                "category_url": final_url,
                "max_products": int(max_products),
                "china_only": bool(china_only),
                "started_at": time.time(),
                "finished_at": None,
            })
            HUB.publish({"type": "status", **self.status()})
            HUB.publish({"type": "log", "level": "INFO", "ts": time.time(),
                         "line": f"Запуск парсинга: {final_url} (лимит {max_products}, only_china={china_only})"})

            if self._watch_thread is None or not self._watch_thread.is_alive():
                self._watch_thread = threading.Thread(target=self._watch, daemon=True)
                self._watch_thread.start()

    def stop(self) -> None:
        if self.manager:
            self.manager.stop_parsing()
            logging.getLogger(__name__).warning("Получена команда остановки парсинга")

    # --- наблюдение за завершением ----------------------------------------- #
    def _watch(self) -> None:
        while True:
            time.sleep(1.0)
            if not self.state["running"] or self.manager is None:
                return
            try:
                alive = bool(self.manager.get_status().get("is_running"))
            except Exception:
                alive = False
            if alive:
                continue

            self.state["running"] = False
            self.state["finished_at"] = time.time()
            self.state["phase"] = "Готово"
            self._release_browser()
            self.state["progress"] = 100
            PROGRESS.phase = "Готово"
            PROGRESS.value = 100
            self._results_cache = self._serialize()
            stats = self._results_cache["stats"]
            self.state["stats"] = stats
            # Автосохранение в базу: иначе результат прогона живёт только до
            # перезапуска дашборда, а при следующем парсе заменяется новым.
            saved = self._persist_sellers()
            if saved:
                stats["saved_to_db"] = saved
            HUB.publish({"type": "done", "stats": stats,
                         "products": self._results_cache["products"]})
            HUB.publish({"type": "status", **self.status()})
            logging.getLogger(__name__).info(
                "Готово: товаров %s, китайских %s, в базу %s",
                stats.get("total"), stats.get("chinese"), saved
            )
            return

    def _persist_sellers(self) -> int:
        """Переносит продавцов из последнего прогона в базу.

        В первую очередь попадают подтверждённые по USCC — реквизиты читает
        seller_parser. Остальные сохраняются как эвристика и в дашборде по
        умолчанию не показываются.
        """
        try:
            from src.crm.store import VERIFY_HEURISTIC, VERIFY_NONE, VERIFY_USCC
            from src.outreach.scoring import SellerMetrics, score_all

            results = (self.manager.last_results if self.manager else None) or {}
            sellers = results.get("sellers") or []
            if not sellers:
                return 0

            by_id = {getattr(s, "seller_id", ""): s for s in sellers if getattr(s, "seller_id", "")}
            links = results.get("links") or {}
            product_by_seller = {}
            for product in results.get("products") or []:
                seller_id = getattr(product, "seller_id", "")
                if seller_id and seller_id not in product_by_seller:
                    url = getattr(product, "seller_link", "") or ""
                    for product_url in links:
                        if getattr(product, "article", "") and product.article in product_url:
                            url = product_url
                            break
                    product_by_seller[seller_id] = url or getattr(product, "seller_link", "")

            metrics = []
            for seller_id, seller in by_id.items():
                uscc = getattr(seller, "uscc", "") or ""
                is_chinese = bool(getattr(seller, "is_chinese", False))
                if uscc:
                    verified = VERIFY_USCC
                elif is_chinese:
                    # Эвристики: в базе хранятся, но в дашборде скрыты по умолчанию
                    verified = VERIFY_HEURISTIC
                else:
                    verified = VERIFY_NONE

                metrics.append(SellerMetrics.from_raw(
                    seller_id=seller_id,
                    orders_count=getattr(seller, "orders_count", ""),
                    reviews_count=getattr(seller, "reviews_count", ""),
                    working_time=getattr(seller, "working_time", ""),
                    average_rating=getattr(seller, "average_rating", ""),
                    shop_name=getattr(seller, "shop_name", "") or "",
                    company_name=getattr(seller, "company_name", ""),
                    is_chinese=bool(uscc) or is_chinese,
                    uscc=uscc,
                    province=getattr(seller, "province", "") or "",
                    registration_kind=getattr(seller, "registration_kind", "unknown") or "unknown",
                    seller_profile_url=f"https://www.ozon.ru/seller/{seller_id}/",
                    sample_product_url=product_by_seller.get(seller_id, ""),
                    verified_by=verified,
                    chinese_reasons=getattr(seller, "chinese_reasons", ""),
                ))

            score_all(metrics)
            return CRUD.store().add_sellers(metrics)
        except Exception as exc:                            # noqa: BLE001
            logging.getLogger(__name__).warning(f"Не удалось сохранить продавцов в базу: {exc}")
            return 0

    # --- результаты --------------------------------------------------------- #
    def _serialize(self) -> dict:
        results = (self.manager.last_results if self.manager else None) or {}
        products = results.get("products") or []
        links = results.get("links") or {}
        sellers = results.get("seller_data") or {}

        rows = []
        for p in products:
            article = getattr(p, "article", "")
            product_url = ""
            for u in links:
                if article and article in u:
                    product_url = u
                    break
            s = sellers.get(getattr(p, "seller_id", ""))
            rows.append({
                "article": article,
                "name": getattr(p, "name", ""),
                "seller": getattr(p, "company_name", ""),
                "company": getattr(s, "company_name", "") if s else "",
                "inn": (getattr(s, "inn", "") if s else "") or getattr(p, "company_inn", ""),
                "country": getattr(p, "seller_country", "") or (getattr(s, "seller_country", "") if s else ""),
                "is_chinese": bool(getattr(p, "is_chinese", False)),
                "confidence": round(float(getattr(p, "chinese_confidence", 0.0) or 0.0), 3),
                "reasons": getattr(p, "chinese_reasons", "") or (getattr(s, "chinese_reasons", "") if s else ""),
                "price": getattr(p, "price", 0),
                "card_price": getattr(p, "card_price", 0),
                "original_price": getattr(p, "original_price", 0),
                "image": getattr(p, "image_url", ""),
                "url": product_url,
                "orders": getattr(s, "orders_count", "") if s else "",
                "rating": getattr(s, "average_rating", "") if s else "",
                "reviews": getattr(s, "reviews_count", "") if s else "",
                "success": bool(getattr(p, "success", False)),
                "error": getattr(p, "error", ""),
            })

        chinese = [r for r in rows if r["is_chinese"]]
        confs = [r["confidence"] for r in chinese]
        elapsed = 0.0
        if self.state["started_at"]:
            end = self.state["finished_at"] or time.time()
            elapsed = end - self.state["started_at"]
        stats = {
            "total": len(rows),
            "chinese": len(chinese),
            "others": len(rows) - len(chinese),
            "failed": len([r for r in rows if not r["success"]]),
            "avg_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
            "elapsed": round(elapsed, 1),
            "output_folder": results.get("output_folder", ""),
        }
        return {"products": rows, "stats": stats}

    def results(self) -> dict:
        if self.state["running"] and not self._results_cache["products"]:
            return {"products": [], "stats": {}}
        if not self._results_cache["products"] and self.manager:
            self._results_cache = self._serialize()
        return self._results_cache

    def status(self) -> dict:
        st = dict(self.state)
        if st["running"]:
            st["phase"] = PROGRESS.phase
            st["progress"] = round(PROGRESS.value, 1)
        return st


SERVICE = Service()


# --------------------------------------------------------------------------- #
#  HTTP-сервер
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OzonParserDashboard/1.0"

    def log_message(self, fmt, *args):        # не засоряем консоль запросами
        pass

    # --- авторизация --------------------------------------------------------- #
    def _has_session(self) -> bool:
        """Есть ли cookie с активной сессией."""
        cookie = self.headers.get("Cookie", "") or ""
        return f"{SESSION_COOKIE}={SESSION_TOKEN}" in cookie

    def _has_basic(self) -> bool:
        """HTTP Basic — оставлен для скриптов, curl и автоматизации."""
        if not AUTH[1]:
            return False
        import base64

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
            user, _, password = decoded.partition(":")
            return user == AUTH[0] and password == AUTH[1]
        except Exception:                                  # noqa: BLE001
            return False

    def _serve_login(self, error: str = "") -> None:
        page = LOGIN_PAGE.replace("__ERROR__", error)
        body = page.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        """Проверяет доступ. Возвращает True, если запрос можно обработать.

        Порядок: сессия в cookie, затем HTTP Basic (для скриптов). Если ни то,
        ни другое — браузеру отдаём страницу входа с формой, а API-клиенту JSON.
        Пароль берётся из .env; пустой пароль означает «авторизация выключена».
        """
        if not AUTH[1]:
            return True

        path = urlparse(self.path).path
        # Сама страница входа и выход авторизации не требуют — иначе получился
        # бы замкнутый круг: чтобы войти, нужно уже быть вошедшим
        if path in ("/api/login", "/api/logout"):
            return True

        if self._has_session() or self._has_basic():
            return True

        wants_html = (
            self.headers.get("Accept", "").find("text/html") >= 0
            or path in ("/", "/index.html")
        )
        if wants_html:
            self._serve_login()
        else:
            body = json.dumps({"error": "Требуется вход"}, ensure_ascii=False).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return False

    # --- утилиты ------------------------------------------------------------ #
    def _query(self) -> dict:
        """Query-параметры запроса в виде словаря."""
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        return {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

    def _crm(self, query: dict) -> SellersApi:
        """CRM выбранного маркетплейса: ?marketplace=wb — база Wildberries.

        Дефолт ozon сохраняет обратную совместимость: старые вызовы UI и
        скрипты с Basic-авторизацией продолжают работать без параметра.
        """
        return CRUD_WB if str(query.get("marketplace", "ozon")) == "wb" else CRUD

    def _crm_for_body(self, data: dict) -> SellersApi:
        """То же для POST: маркетплейс приходит в теле запроса."""
        return CRUD_WB if str(data.get("marketplace", "ozon")) == "wb" else CRUD

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            return self._send_json({"error": f"нет файла {path.name}"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- GET ---------------------------------------------------------------- #
    def do_GET(self):
        try:
            self._do_get()
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Ошибка обработки GET %s", self.path)
            self._send_json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def _do_get(self):
        path = urlparse(self.path).path
        if path in PUBLIC_STATIC:
            name, content_type = PUBLIC_STATIC[path]
            return self._send_file(WEB_DIR / name, content_type)
        if not self._check_auth():
            return
        # Один замок на все wb-запросы: пока WB_ENABLED=0, никакой обработчик
        # маркетплейса недоступен, даже через прямые вызовы API.
        if self._query().get("marketplace") == "wb" and not WB_ENABLED:
            return self._send_json(
                {"error": "WB отключён (вернуть: WB_ENABLED=1 в .env)", "wb_enabled": False}, 400)
        if path in ("/", "/index.html"):
            return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/sellers":
            return self._send_json(self._crm(self._query()).sellers(self._query()))
        if path == "/api/send/template":
            return self._send_json(SEND.read_template())
        if path == "/api/send/preview-message":
            return self._send_json(SEND.preview_message(self._query().get("seller_id")))
        if path == "/api/summary":
            return self._send_json(self._crm(self._query()).summary())
        if path == "/api/stats":
            # Сводка для вкладки «Обзор»: статусы продавцов, отправки по дням
            # и лента последних сообщений.
            from src.crm.store import REPLY_CATEGORY_LABELS, STATUS_LABELS
            store = self._crm(self._query()).store()
            return self._send_json({
                "status_counts": store.status_counts(),
                "sent_by_day": store.sent_by_day(days=15),
                "received_by_day": store.received_by_day(days=15),
                "replies_recent": store.replies_recent(limit=10),
                "inbox_auto": INBOX_AUTO.status(),
                "recent": store.recent_messages(limit=14),
                "status_labels": STATUS_LABELS,
                # Коммуникации: категории ответов и передача менеджеру
                "comm_counts": store.communication_counts(),
                "reply_counts": store.category_counts(),
                "reply_labels": REPLY_CATEGORY_LABELS,
                # CRM: стадии сделки и «пора напомнить» — кто ответил и замолчал
                "stage_counts": store.stage_counts(),
                "stage_labels": store.STAGE_LABELS,
                "reminders": store.reminders(days=3, limit=12),
                # Живость продавцов: кто отвечает на отзывы, кто молчит
                "vitality_counts": store.vitality_counts(),
            })
        if path == "/api/seller/detail":
            seller_id = self._query().get("seller_id", "")
            detail = self._crm(self._query()).detail(seller_id)
            return self._send_json(detail or {"error": "Продавец не найден"},
                                   200 if detail else 404)
        if path == "/api/sellers/export.csv":
            marketplace = str(self._query().get("marketplace", "ozon"))
            body = self._crm(self._query()).export_csv(self._query()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{marketplace}-sellers.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/sellers/replies.xlsx":
            # Выгрузка «ответивших»: карточка + последняя переписка — для
            # менеджера и планшета.
            from src.crm.xlsx_export import replied_xlsx

            body = replied_xlsx(CRUD.store().replied_export_rows())
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header("Content-Disposition", 'attachment; filename="otvetivshie.xlsx"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/harvest":
            return self._send_json({
                **HARVEST.status(),
                "default_url": CHINA_SECTION_URL,
                # Сохранённые ссылки и параметры обоих сборов: интерфейс
                # подставляет их в поля при открытии вкладки
                "settings": load_harvest_settings(),
            })
        if path == "/api/night":
            return self._send_json(NIGHT.status())
        if path == "/api/send":
            return self._send_json({
                "state": SEND.status(),
                "preflight": SEND.preflight(),
                "settings": SEND.settings(),
            })
        if path == "/api/seller/history":
            seller_id = self._query().get("seller_id", "")
            rows = self._crm(self._query()).store().message_history(seller_id)
            return self._send_json({"rows": [dict(r) for r in rows]})
        if path == "/api/worker":
            # Статус воркера маркетплейса: жив, стейт-файл, хвост лога.
            marketplace = str(self._query().get("marketplace", "ozon"))
            payload = WORKERS.status(marketplace)
            if marketplace == "wb":
                from src.parsers.wb.harvest import DEFAULT_QUERY as WB_DEFAULT_QUERY

                payload["default_url"] = WB_DEFAULT_QUERY
            else:
                payload["default_url"] = CHINA_SECTION_URL
            return self._send_json(payload)
        if path == "/api/status":
            return self._send_json({**SERVICE.status(), "harvest": HARVEST.status()})
        if path == "/api/results":
            return self._send_json(SERVICE.results())
        if path == "/api/health":
            return self._send_json({"ok": True, "wb_enabled": WB_ENABLED})
        if path == "/api/parser-check":
            SERVICE.check_parser()
            return self._send_json({
                "ready": SERVICE.state["parser_ready"],
                "error": SERVICE.state["parser_error"],
            })
        if path == "/api/events":
            return self._serve_events()
        self._send_json({"error": "not found"}, 404)

    def _serve_events(self) -> None:
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    # --- POST --------------------------------------------------------------- #
    def do_POST(self):
        try:
            self._do_post()
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Ошибка обработки POST %s", self.path)
            self._send_json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def _do_post(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path

        # Вход и выход обрабатываем ДО разбора JSON: форма шлёт
        # application/x-www-form-urlencoded, и общий разбор отклонял бы её
        # как «некорректный JSON».

        if path == "/api/login":
            from urllib.parse import parse_qs

            # Замок от перебора пароля: после серии неудач с одного адреса
            # вход замолкает на 10 минут. Лимит мягкий — живой человек его
            # не заметит, брутфорс остановит.
            client_ip = self.client_address[0]
            now = time.time()
            while LOGIN_FAILS and now - LOGIN_FAILS[0][0] > LOGIN_BLOCK_SECONDS:
                LOGIN_FAILS.popleft()
            blocked_until = LOGIN_BLOCKS.get(client_ip, 0)
            if now < blocked_until:
                retry = int(blocked_until - now) + 1
                return self._send_json(
                    {"error": f"Слишком много попыток — подождите {retry} с"}, 429)

            form_len = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(form_len).decode("utf-8", "replace") if form_len else ""
            data = {k: v[0] for k, v in parse_qs(raw).items() if v}
            if not AUTH[1] or data.get("password") == AUTH[1]:
                LOGIN_BLOCKS.pop(client_ip, None)
                # Secure-флаг только через HTTPS (прокси Caddy помечает схему):
                # на локальном http cookie без Secure, иначе браузер его съест.
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                body = "<meta http-equiv=\"refresh\" content=\"0;url=/\">".encode("utf-8")
                self.send_response(303)
                self.send_header("Set-Cookie",
                                 f"{SESSION_COOKIE}={SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Lax{secure}")
                self.send_header("Location", "/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return self._send_json({"ok": True})
            fails = LOGIN_FAILS_BY_IP[client_ip] = LOGIN_FAILS_BY_IP.get(client_ip, 0) + 1
            LOGIN_FAILS.append((now, client_ip))
            if fails >= LOGIN_MAX_FAILS:
                LOGIN_BLOCKS[client_ip] = now + LOGIN_BLOCK_SECONDS
                LOGIN_FAILS_BY_IP.pop(client_ip, None)
                logging.getLogger(__name__).warning(
                    "Вход заблокирован на %s с на %s: %d неудачных попыток",
                    LOGIN_BLOCK_SECONDS, client_ip, fails)
            return self._serve_login("Неверный пароль")

        if path == "/api/logout":
            self.send_response(303)
            self.send_header("Set-Cookie",
                             f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._send_json({"error": "Некорректный JSON"}, 400)

        # Зеркало GET-замка: любые wb-команды (запуск воркера, правки CRM)
        # отклоняются, пока маркетплейс выключен.
        if str(data.get("marketplace", "ozon")) == "wb" and not WB_ENABLED:
            return self._send_json(
                {"error": "WB отключён (вернуть: WB_ENABLED=1 в .env)", "wb_enabled": False}, 400)

        if path == "/api/seller/note":
            seller_id = str(data.get("seller_id", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            return self._send_json(self._crm_for_body(data).set_note(seller_id, str(data.get("note", ""))))

        if path == "/api/seller/status":
            seller_id = str(data.get("seller_id", "")).strip()
            status = str(data.get("status", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            try:
                return self._send_json(self._crm_for_body(data).set_status(seller_id, status))
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/seller/stage":
            seller_id = str(data.get("seller_id", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            try:
                return self._send_json({"ok": self._crm_for_body(data).store().set_stage(seller_id, str(data.get("stage", "")))})
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/seller/category":
            # Ручная правка категории ответа: классификатор бывает неточен,
            # а решения по продавцу принимает человек.
            seller_id = str(data.get("seller_id", "")).strip()
            category = str(data.get("category", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            result = self._crm_for_body(data).set_category(seller_id, category)
            return self._send_json(result, 200 if result.get("ok") else 400)

        if path == "/api/seller/handover":
            # Передача заинтересованному продавцу менеджеру: отметка времени
            # в базе, продавец остаётся в общей таблице.
            seller_id = str(data.get("seller_id", "")).strip()
            if not seller_id:
                return self._send_json({"error": "Не указан seller_id"}, 400)
            result = self._crm_for_body(data).handover(seller_id)
            if result.get("ok"):
                HUB.log(f"Передан менеджеру: {seller_id}")
            return self._send_json(result)

        if path == "/api/seller/reply":
            # Ответ из дашборда: открываем чат продавца в общем Chrome и
            # отправляем текст. Синхронно (~20–40 с) — кнопка в интерфейсе
            # крутится, пока не придёт ответ.
            if str(data.get("marketplace", "ozon")) == "wb":
                return self._send_json(
                    {"error": "У Wildberries нет чата с продавцом — рассылка WB появится отдельно"},
                    400)
            seller_id = str(data.get("seller_id", "")).strip()
            text = str(data.get("text", "")).strip()
            if not seller_id or not text:
                return self._send_json({"error": "Нужны seller_id и текст"}, 400)
            if not BROWSER_LOCK.acquire(blocking=False):
                return self._send_json(
                    {"error": "Браузер занят другой задачей — дождитесь её завершения"}, 400)
            try:
                from src.crm.reply import reply_to_seller

                result = reply_to_seller(SEND.DATA_DIR, CRUD.store(), seller_id, text)
                HUB.log(f"Ответ из дашборда: {seller_id} — текст ушёл в чат Ozon")
                from src.notify import telegram as tg

                shop = (CRUD.detail(seller_id) or {}).get("shop_name") or seller_id
                tg.send(f"💬 Ответ ушёл продавцу {shop}:\n{text[:400]}")
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": str(exc)[:300]}, 500)
            finally:
                BROWSER_LOCK.release()
            return self._send_json(result)

        if path == "/api/sellers/delete":
            ids = data.get("seller_ids") or []
            if not isinstance(ids, list) or not ids:
                return self._send_json({"error": "Не выбрано ни одной записи"}, 400)
            allow_verified = bool(data.get("allow_verified"))
            store = self._crm_for_body(data).store()
            targets = [str(x) for x in ids]

            # Подтверждённых без явного разрешения не удаляем: это собранная
            # база, и случайное выделение не должно её сносить. Считаем, сколько
            # таких, и просим подтвердить отдельно.
            all_rows = store.list_sellers(limit=1_000_000, include_unverified=True)
            by_id = {r["seller_id"]: r["verified_by"] for r in all_rows}
            verified = [x for x in targets
                        if by_id.get(x) in ("uscc", "warehouse")]
            if verified and not allow_verified:
                return self._send_json({
                    "error": f"Среди выбранных {len(verified)} подтверждённых продавцов",
                    "verified_count": len(verified),
                    "need_confirm": True,
                }, 409)

            deleted = store.delete_sellers(targets)
            return self._send_json({"deleted": deleted, "verified_deleted": len(verified)})

        if path == "/api/sellers/purge":
            ids = data.get("seller_ids") or []
            if not isinstance(ids, list) or not ids:
                return self._send_json({"error": "Не выбрано ни одной записи"}, 400)
            # Удаляем только неподтверждённых: подтверждённых сносить по этой
            # кнопке нельзя, иначе случайное выделение уничтожит рабочие данные.
            store = self._crm_for_body(data).store()
            safe = [str(x) for x in ids]
            rows = store.list_sellers(limit=1_000_000, tiers=["none", "heuristic"])
            allowed = {r["seller_id"] for r in rows}
            targets = [x for x in safe if x in allowed]
            if not targets:
                return self._send_json({"error": "Среди выбранных нет неподтверждённых"}, 400)
            return self._send_json({"deleted": store.delete_sellers(targets),
                                    "skipped": len(safe) - len(targets)})

        if path == "/api/worker/start":
            # Воркер запускается отдельным процессом. Для Ozon нужен браузер:
            # он общий с legacy-задачами дашборда, поэтому одновременный запуск
            # запрещён (те же проверки, что делает ночной сбор перед стартом).
            marketplace = str(data.get("marketplace", "")).strip()
            if marketplace not in ("ozon", "wb"):
                return self._send_json({"error": "marketplace: ozon|wb"}, 400)
            if marketplace == "ozon":
                if WORKERS.running("ozon"):
                    return self._send_json({"error": "Воркер Ozon уже работает"}, 400)
                if HARVEST.status().get("running"):
                    return self._send_json({"error": "Идёт обычный сбор — воркер подождёт"}, 400)
                if NIGHT.running():
                    return self._send_json({"error": "Идёт ночной сбор — воркер подождёт"}, 400)
                if SEND.status().get("running"):
                    return self._send_json({"error": "Идёт рассылка — воркер подождёт"}, 400)
            try:
                WORKERS.start(marketplace, {
                    "url": str(data.get("url", "")).strip(),
                    "pages": max(1, min(int(data.get("pages") or 10), 1000)),
                    "start_page": max(1, int(data.get("start_page") or 1)),
                    "delay": data.get("delay"),
                    "refresh_days": max(0, min(int(data.get("refresh_days") or 7), 365)),
                })
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True, **WORKERS.status(marketplace)})

        if path == "/api/worker/stop":
            marketplace = str(data.get("marketplace", "")).strip()
            if not WORKERS.stop(marketplace):
                return self._send_json({"error": f"Воркер {marketplace} не запущен"}, 400)
            return self._send_json({"ok": True})

        if path == "/api/harvest":
            # Legacy in-process сбор: конкурирует с воркером Ozon за браузер.
            if WORKERS.running("ozon"):
                return self._send_json({"error": "Работает воркер Ozon — сначала остановите его"}, 400)
            try:
                delay = data.get("delay")
                harvest_url = str(data.get("url", "")).strip()
                pages = int(data.get("pages") or 10)
                HARVEST.start(
                    harvest_url,
                    pages,
                    delay=float(delay) if delay is not None else None,
                )
                # Что запустили — то и запоминаем: поля в интерфейсе должны
                # пережить перезапуск браузера. Ошибка сохранения не срывает
                # уже запущенный сбор.
                try:
                    saved = {"url": harvest_url, "pages": pages}
                    if delay is not None:
                        saved["delay"] = float(delay)
                    save_harvest_settings({"harvest": saved})
                except ValueError:
                    pass
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True})

        if path == "/api/harvest/stop":
            HARVEST.stop()
            return self._send_json({"ok": True})

        if path == "/api/harvest/settings":
            try:
                return self._send_json({"ok": True, "settings": save_harvest_settings(data)})
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/night/start":
            if WORKERS.running("ozon"):
                return self._send_json({"error": "Работает воркер Ozon — сначала остановите его"}, 400)
            try:
                night_url = str(data.get("url") or "").strip()
                chunk = max(1, min(int(data.get("chunk_pages") or 10), 100))
                until = str(data.get("until") or "08:00").strip() or "08:00"
                pause = max(5.0, min(float(data.get("pause") or 180), 3600.0))
                refresh_days = max(0, min(int(data.get("refresh_days") or 7), 365))
                # Настройки валидируются и сохраняются ДО запуска: кривое
                # «работать до» не должно запускать сбор, который упадёт сразу
                save_harvest_settings({"night": {
                    "url": night_url, "chunk_pages": chunk, "until": until,
                    "pause": pause, "refresh_days": refresh_days,
                }})
                NIGHT.start(chunk_pages=chunk, until=until, pause=pause,
                            refresh_days=refresh_days, url=night_url)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True, **NIGHT.status()})

        if path == "/api/night/stop":
            NIGHT.stop()
            return self._send_json({"ok": True})

        if path == "/api/send":
            try:
                limit = data.get("limit")
                SEND.start(
                    dry_run=data.get("dry_run"),
                    limit=int(limit) if limit else None,
                )
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True})

        if path == "/api/send/stop":
            SEND.stop()
            return self._send_json({"ok": True})

        if path == "/api/send/stopfile":
            active = bool(data.get("active"))
            path_file = SEND.DATA_DIR / SEND.config().stop_file
            try:
                SEND.DATA_DIR.mkdir(parents=True, exist_ok=True)
                if active:
                    path_file.write_text(
                        f"Остановлено из дашборда {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                        encoding="utf-8",
                    )
                elif path_file.exists():
                    path_file.unlink()
            except OSError as exc:
                return self._send_json({"error": f"Не удалось: {exc}"}, 500)
            return self._send_json({"ok": True, "active": path_file.exists()})

        if path == "/api/send/check-login":
            from src.outreach.sender import check_login_live
            live = check_login_live(SEND.DATA_DIR, on_progress=HUB.log)
            return self._send_json(live)

        if path == "/api/inbox/check":
            # Проверка входящих открывает вкладку в общем Chrome: как и сбор
            # с отправкой, держит замок, чтобы задачи не мешали друг другу.
            if not BROWSER_LOCK.acquire(blocking=False):
                return self._send_json(
                    {"error": "Браузер занят другой задачей — дождитесь её завершения"}, 400)
            try:
                from src.crm.inbox import InboxChecker
                result = InboxChecker(CRUD.store(), data_dir=SEND.DATA_DIR).check()
                HUB.log(
                    f"Входящие: чатов {result['chats']}, с непрочитанными {result['unread']}, "
                    f"пометили «ответил» {len(result['marked'])}"
                    + (f", не совпали с базой: {', '.join(result['unmatched'])}"
                       if result["unmatched"] else ""))
                # Кто ответил — сразу в Telegram: не нужно держать дашборд открытым
                from src.notify import telegram as tg

                for marked in result["marked"]:
                    tg.send(f"💬 Ответил: {marked['shop']}\n{marked['text'][:400]}")
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Не удалось проверить: {exc}"}, 500)
            finally:
                BROWSER_LOCK.release()
            return self._send_json(result)

        if path == "/api/send/template":
            result = SEND.save_template(str(data.get("body", "")))
            code = 200 if result.get("ok") else 400
            return self._send_json(result, code)

        if path == "/api/send/settings":
            try:
                return self._send_json({"settings": SEND.save_settings(data)})
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/sellers/requeue":
            ids = data.get("seller_ids") or []
            count = CRUD.store().requeue_failed([str(x) for x in ids] or None)
            return self._send_json({"requeued": count})

        if path == "/api/start":
            try:
                SERVICE.start(
                    str(data.get("url", "")).strip(),
                    int(data.get("max_products") or 50),
                    bool(data.get("china_only", True)),
                )
            except RuntimeError as exc:
                return self._send_json({"error": str(exc)}, 400)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 409)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Ошибка запуска: {exc}"}, 500)
            return self._send_json({"ok": True})

        if path == "/api/stop":
            SERVICE.stop()
            return self._send_json({"ok": True})

        self._send_json({"error": "not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Дашборд Ozon-парсера")
    parser.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт")
    parser.add_argument("--open", action="store_true", help="открыть браузер")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), HubLogHandler()],
    )
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("WDM").setLevel(logging.WARNING)

    is_local = args.host in ("127.0.0.1", "localhost")
    url = f"http://{'127.0.0.1' if is_local else args.host}:{args.port}/"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  Дашборд Ozon-парсера: {url}\n  Остановка: Ctrl+C\n", flush=True)

    SERVICE.check_parser()
    # Автопроверка чатов открывает браузер с Ozon: на машине, где дашборд
    # нужен только для правок кода (OZON_INBOX_AUTO=0 в config.txt или env),
    # её отключают — иначе Chrome с Ozon выскакивает каждые полчаса.
    inbox_disabled = False
    try:
        from src.utils.config_loader import read_config

        raw = (os.environ.get("OZON_INBOX_AUTO")
               or (read_config().get("OZON_INBOX_AUTO") or "")).strip().lower()
        inbox_disabled = raw in ("0", "false", "no", "off")
    except Exception:                                        # noqa: BLE001
        pass
    if inbox_disabled:
        logging.getLogger(__name__).info(
            "Автопроверка чатов отключена (OZON_INBOX_AUTO=0)")
        HUB.log("Автопроверка чатов отключена (OZON_INBOX_AUTO=0)")
    else:
        INBOX_AUTO.start()
    resumed = resume_night_after_restart()
    if resumed:
        # В HUB попадёт через HubLogHandler: прямой вызов продублировал бы строку
        logging.getLogger(__name__).info(resumed)
    if args.open or is_local:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера...", flush=True)
    finally:
        SERVICE.stop()
        server.server_close()


if __name__ == "__main__":
    main()