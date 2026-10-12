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
from datetime import datetime, timedelta, timezone
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

DEFAULT_PORT = int(os.environ.get("PORT", "8000"))

# База продавцов: мини-CRM с заметками и статусами
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "sellers.sqlite3"

# Авто-инициализация базы при первом запуске в облаке (Railway / Docker)
DATA_DIR.mkdir(parents=True, exist_ok=True)
if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
    seed_path = REPO_ROOT / "seed_data" / "sellers.sqlite3"
    if seed_path.exists() and seed_path.stat().st_size > 0:
        import shutil
        shutil.copy2(seed_path, DB_PATH)

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
        # Ниша: фильтр по актуальной категории товара (multi, как провинции)
        niches = multi("niche")

        store = self.store()
        kwargs = dict(statuses=statuses, provinces=provinces, search=search, tiers=tiers,
                      reply_categories=categories, vitality=vitality, niches=niches)
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
            "niche_facets": store.niche_facets(),
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
            niches=multi("niche"),
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
        writer.writerow([title for _, title in columns] + ["Ниша", "Живость"])
        for row in rows:
            v = vitality_payload(int(row["reviews_seen"] or 0), int(row["replies_seen"] or 0))
            if v["seen"] <= 0:
                vitality_text = ""
            elif v["category"] == CAT_UNKNOWN:
                vitality_text = "мало данных"
            else:
                vitality_text = f'{v["label"]} {v["replies"]}/{v["seen"]}'
            writer.writerow([row[key] for key, _ in columns]
                            + [row["niche"] or "", vitality_text])
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
CHINA_SECTION_URL = "GLOBAL"

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

    def start(self, url: str, pages: int, delay: Optional[float] = None,
              loop: bool = False) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Сбор уже идёт")

            url = (url or "").strip() or CHINA_SECTION_URL
            if url != "GLOBAL" and "ozon.ru" not in url:
                raise ValueError("Укажите ссылку на раздел Ozon или GLOBAL")
            # Лимит 500 держим вровень с полем «страниц» в dashboard/web/index.html
            # (max="500"); сам харвест страницы не ограничивает, обход останавливается
            # сам, когда Ozon перестаёт отдавать новые товары.
            pages = max(1, min(int(pages or 10), 500))

            self._stop.clear()
            self._delay = delay
            self.loop = bool(loop)
            self.state.update({
                "running": True, "url": url, "pages": pages,
                "loop": bool(loop),
                "phase": "Запускаю браузер", "progress": 0,
                "error": "", "stats": {},
                "started_at": time.time(), "finished_at": None,
            })

        self._thread = threading.Thread(
            target=self._run,
            args=(url, pages, getattr(self, "_delay", None), bool(loop)),
            daemon=True,
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

    def _run(self, url: str, pages: int, delay: Optional[float] = None,
             loop: bool = False) -> None:
        """Один или (loop=True) бесконечная серия обходов раздела.

        Режим «по кругу 24/7» (запрос оператора 03.10.2026): обход закончился —
        пауза 10 минут — новый обход той же ссылки из НАСТРОЕК (ссылку можно
        менять в панели между обходами). Продавцы-дубли отсекаются базой,
        в очередь попадают только новые. Ошибки раунда не убивают серию:
        пауза и повтор. Выход — только стоп-кнопка.
        """
        harvest_log_file = REPO_ROOT / "logs" / f"harvest_{datetime.now().strftime('%Y-%m-%d')}.log"
        harvest_log_file.parent.mkdir(parents=True, exist_ok=True)

        session_products = 0
        session_sellers = 0
        session_uscc = 0
        session_errors = 0
        session_chunks = 0

        def log(message: str) -> None:
            HUB.publish({"type": "log", "line": message})
            try:
                with open(harvest_log_file, "a", encoding="utf-8") as f:
                    ts = time.strftime("%H:%M:%S")
                    f.write(f"[{ts}] {message}\n")
            except Exception:
                pass

        def progress(done: int, total: int, phase: str) -> None:
            # Три фазы разной длины: сбор товаров, определение продавцов,
            # проверка реквизитов. В одну шкалу их сводим с весами.
            weights = {"Собираю товары": (0, 40), "Определяю продавцов": (40, 35),
                       "Проверяю реквизиты": (75, 25)}
            start, span = weights.get(phase, (0, 0))
            value = start + (span * done / total if total else 0)
            self.state["phase"] = phase
            self.state["progress"] = round(min(value, 100), 1)

            h_stats = getattr(harvester, "stats", None) if 'harvester' in locals() else None
            cur_prod = session_products + (h_stats.products if h_stats else 0)
            cur_sellers = session_sellers + (h_stats.sellers_found if h_stats else 0)
            cur_uscc = session_uscc + (h_stats.sellers_verified_uscc if h_stats else 0)
            cur_errs = session_errors + (len(h_stats.errors) if h_stats else 0)

            now_sub = phase
            if phase == "Собираю товары":
                now_val = f"стр. {done}/{total}" if total else f"стр. {done}"
                next_val = f"стр. {done + 1}" if done < total else f"раунд #{round_no}"
            elif phase == "Определяю продавцов":
                now_val = f"{done}/{total}" if total else str(done)
                next_val = f"найдено: {cur_sellers}"
            elif phase == "Проверяю реквизиты":
                now_val = f"{done}/{total}" if total else str(done)
                next_val = f"USCC: {cur_uscc}"
            else:
                now_val = f"{done}/{total}" if total else str(done)
                next_val = f"раунд #{round_no}"

            self.state["live"] = {
                "phase": phase,
                "now_value": now_val,
                "now_sub": now_sub,
                "next": next_val,
                "products": cur_prod,
                "sellers": cur_sellers,
                "uscc": cur_uscc,
                "chunks": session_chunks + 1,
                "errors": cur_errs,
            }
            HUB.publish({"type": "harvest", **self.state})

        class HarvestStopped(Exception):
            pass

        def pause(seconds: int, text: str) -> bool:
            """Пауза с проверкой стопа и живым фазовым текстом. False — выход."""
            deadline = time.time() + seconds
            last_shown = -1
            while time.time() < deadline:
                if self._stop.is_set():
                    return False
                left = int(deadline - time.time())
                if left // 30 != last_shown:
                    last_shown = left // 30
                    self.state["phase"] = f"{text}: {left // 60} мин {left % 60:02d} с"
                    HUB.publish({"type": "harvest", **self.state})
                time.sleep(1)
            return True

        round_no = 0
        try:
            while True:
                round_no += 1
                if loop and round_no > 1:
                    # Ссылку берём из настроек КАЖДЫЙ обход (кроме первого, чтобы
                    # избежать гонки при сохранении файла): её можно поменять
                    # в панели между обходами, не останавливая серию
                    saved = (load_harvest_settings().get("harvest") or {})
                    url = (saved.get("url") or "").strip() or url

                # Браузер на время обхода. В цикле ждём освобождения с
                # проверкой стопа, а не отваливаемся
                while not BROWSER_LOCK.acquire(blocking=False):
                    if self._stop.is_set():
                        raise HarvestStopped()
                    self.state["phase"] = "По кругу: жду освобождения браузера"
                    HUB.publish({"type": "harvest", **self.state})
                    time.sleep(5)
                self._holds_browser = True

                try:
                    from src.crm.harvest import ChinaSellerHarvester, OzonPageClient
                    from src.crm.store import SellerStore
                    from src.utils.browser import create_browser_manager

                    log(f"Обход #{round_no}: раздел {url}")
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
                        
                        if url == "GLOBAL":
                            from src.crm.harvest import HarvestStats
                            stats = HarvestStats()
                            global_urls = [
                                "/category/elektronika-15500/",
                                "/category/odezhda-obuv-i-aksessuary-7500/",
                                "/category/dom-i-sad-14500/",
                                "/category/detskie-tovary-7000/",
                                "/category/krasota-i-zdorove-6500/",
                                "/category/avtotovary-8500/",
                                "/category/sport-i-otdyh-11000/",
                                "/category/stroitelstvo-i-remont-9700/"
                            ]
                            for idx, g_url in enumerate(global_urls, 1):
                                if self._stop.is_set():
                                    break
                                log(f"GLOBAL-обход #{round_no} - категория {idx}/{len(global_urls)}: {g_url}")
                                step_stats = harvester.run(g_url, pages=pages)
                                stats.pages += step_stats.pages
                                stats.products += step_stats.products
                                stats.sellers_found += step_stats.sellers_found
                                stats.sellers_verified_uscc += step_stats.sellers_verified_uscc
                                stats.cancelled = step_stats.cancelled
                        else:
                            stats = harvester.run(url, pages=pages)
                            
                    finally:
                        try:
                            manager.close()
                        except Exception:                    # noqa: BLE001
                            pass
                except HarvestStopped:
                    raise
                except Exception as exc:                     # noqa: BLE001
                    self._release_browser()
                    if not loop or self._stop.is_set():
                        raise
                    log(f"Ошибка обхода: {str(exc)[:200]} — по кругу повторю через 10 мин")
                    if not pause(600, "По кругу: пауза после ошибки"):
                        raise HarvestStopped()
                    continue

                self._release_browser()
                session_products += stats.products
                session_sellers += stats.sellers_found
                session_uscc += stats.sellers_verified_uscc
                session_errors += len(stats.errors)
                session_chunks += 1

                self.state.update({
                    "phase": "Остановлено" if stats.cancelled else ("Пауза перед следующим кругом" if loop else "Готово"),
                    "progress": 100,
                    "stats": {
                        "pages": stats.pages,
                        "products": session_products,
                        "sellers": session_sellers,
                        "uscc": session_uscc,
                        "warehouse": stats.sellers_verified_warehouse,
                        "rejected": stats.sellers_rejected,
                        "requests": stats.requests,
                        "cancelled": bool(stats.cancelled),
                        "errors": list(stats.errors)[:20],
                        "errors_count": session_errors,
                        "chunks": session_chunks,
                        "round": round_no,
                    },
                    "live": {
                        "phase": "пауза" if loop else ("остановлен" if stats.cancelled else "готово"),
                        "now_value": f"раунд #{round_no}",
                        "now_sub": "проход завершен",
                        "next": f"раунд #{round_no + 1}" if loop else "—",
                        "products": session_products,
                        "sellers": session_sellers,
                        "uscc": session_uscc,
                        "chunks": session_chunks,
                        "errors": session_errors,
                    },
                    "finished_at": time.time(),
                })
                HUB.publish({"type": "harvest", **self.state})
                if stats.cancelled:
                    log(
                        f"Обход остановлен по запросу: страниц {stats.pages}, "
                        f"товаров {stats.products}, продавцов проверено {stats.sellers_found}"
                    )
                else:
                    log(
                        f"Обход #{round_no} завершён: страниц {stats.pages}, "
                        f"товаров {stats.products}, продавцов {stats.sellers_found} "
                        f"(по USCC {stats.sellers_verified_uscc}, "
                        f"по складу {stats.sellers_verified_warehouse})"
                    )
                HUB.publish({"type": "harvest", **self.state})
                HUB.publish({"type": "harvest_done", "stats": self.state["stats"]})

                # Выход из серии: не цикл, оператор нажал стоп, обход был
                # прерван. Иначе — пауза и следующий обход.
                if not loop or stats.cancelled or self._stop.is_set():
                    return
                log("По кругу 24/7: следующий обход через 10 минут")
                if not pause(600, "По кругу: пауза между обходами"):
                    return
        except HarvestStopped:
            self.state.update({
                "running": False, "phase": "Остановлено", "progress": 100,
                "finished_at": time.time(),
            })
            HUB.publish({"type": "harvest", **self.state})
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
        finally:
            self._release_browser()


HARVEST = HarvestRunner()

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
    if harvest["url"] and harvest["url"] != "GLOBAL" and "ozon.ru" not in harvest["url"]:
        raise ValueError("Ссылка обычного сбора должна вести на ozon.ru или быть GLOBAL")
    harvest["pages"] = min(max(harvest["pages"], 1), 500)
    harvest["delay"] = min(max(harvest["delay"], 0.0), 10.0)

    if night["url"] and night["url"] != "GLOBAL" and "ozon.ru" not in night["url"]:
        raise ValueError("Ссылка ночного сбора должна вести на ozon.ru или быть GLOBAL")
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
            # Ночной сбор — отдельный процесс: BROWSER_LOCK его не останавливает,
            # а второй Chrome на том же профиле ломает обе задачи (аудит)
            if NIGHT.running():
                self.state["last_result"] = "пропущено — идёт ночной сбор"
                return
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
                    hot = marked.get("category") in ("interested", "positive", "needs_info", "negotiation")
                    if hot:
                        tg.send("🔥 ГОРЯЧИЙ ЛИД: " + marked["shop"]
                                + "\nКатегория: " + marked.get("category_label", "")
                                + "\n" + marked["text"][:400])
                    # Негорячие ответы (отказ, нейтральное) — в статистику и
                    # журнал, без пуша: менеджер не должен тонуть в шуме.
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

    def cap_today(self) -> int:
        """Действующий дневной лимит на сегодня: разгон, джиттер, замедление.

        Падение на любом шаге (например, база занята) — не повод ронять
        /api/send: отдаём базовый лимит без множителей.
        """
        from src.outreach.sender import effective_daily_cap, ramp_start_date

        config = self.config()
        now = datetime.now()
        try:
            ramp_started = (ramp_start_date(self.DATA_DIR, now.date())
                            if config.ramp_days > 0 else None)
            store = CRUD.store()
            feedback = (store.outreach_feedback(days=7)
                        if config.adaptive_slowdown
                        and hasattr(store, "outreach_feedback") else None)
        except Exception:                                    # noqa: BLE001
            return max(config.daily_cap, 1)
        return effective_daily_cap(config, now, feedback=feedback,
                                   ramp_started=ramp_started)

    def template(self):
        from src.outreach.templates import TemplateError, TemplatePool
        try:
            return TemplatePool.load(self.TEMPLATES_DIR), ""
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

    def import_templates(self, text: str, mode: str = "replace") -> dict:
        """Разбирает вставленный набор текстов и раскладывает по файлам.

        replace — прежние *.txt переезжают в templates/archive/ (ничего не
        удаляется, вернуть можно обратным переносом), новые становятся всей
        ротацией. append — новые добавляются к существующим. Пул читает
        только *.txt в корне каталога, поэтому архив — просто подкаталог.
        Строка отписки добавляется к вариантам, где её нет: без неё
        отправка не запустится (ст. 18 ФЗ «О рекламе»).
        """
        import re as _re

        from src.outreach.templates import (
            MessageTemplate,
            TemplateError,
            ensure_optout,
            parse_template_set,
        )

        self.TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        try:
            parsed = parse_template_set(text)
        except TemplateError as exc:
            return {"ok": False, "error": str(exc)}

        mode = mode if mode in ("replace", "append") else "replace"
        archived = 0
        if mode == "replace":
            archive = self.TEMPLATES_DIR / "archive"
            archive.mkdir(exist_ok=True)
            for path in self.TEMPLATES_DIR.glob("*.txt"):
                if path.is_file():
                    path.rename(archive / path.name)
                    archived += 1

        taken = {p.name for p in self.TEMPLATES_DIR.glob("*.txt")}
        imported, warnings = [], []
        for num, template in enumerate(parsed, start=1):
            body, added_optout = ensure_optout(template.body)
            candidate = MessageTemplate(name=template.name, body=body)
            try:
                candidate.validate()
            except TemplateError as exc:
                warnings.append(str(exc))
                continue
            slug = _re.sub(r"[^\w\-]+", "_", template.name).strip("_")[:48] or "текст"
            base = f"n{num:02d}_{slug}"
            filename = base + ".txt"
            i = 1
            while filename in taken:
                i += 1
                filename = f"{base}_{i}.txt"
            taken.add(filename)
            (self.TEMPLATES_DIR / filename).write_text(candidate.body, encoding="utf-8")
            imported.append(template.name + (" (+отписка)" if added_optout else ""))

        if not imported:
            return {"ok": False,
                    "error": "ни один вариант не прошёл проверку: " + "; ".join(warnings)}
        return {"ok": True, "imported": imported, "archived": archived,
                "active": len(taken), "warnings": warnings}

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
            # Показываем тот же текст, который уйдёт следующим: пул выбирает его
            # по журналу отправок, поэтому предпросмотр не расходится с делом.
            usage = store.template_usage() if hasattr(store, "template_usage") else {}
            last = store.last_template_used() if hasattr(store, "last_template_used") else ""
            chosen = template.pick(usage, avoid=last) if hasattr(template, "pick") else template
            return {
                "seller_id": row["seller_id"],
                "shop_name": row["shop_name"] or row["seller_id"],
                "orders": row["orders"],
                "text": chosen.render(dict(row)),
                "template_name": getattr(chosen, "name", ""),
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

            # Проверки других запущенных задач убраны: WAL-режим базы данных 
            # и изоляция вкладок браузера (_open_own_tab / context.new_page) 
            # позволяют рассылке и сбору работать одновременно.
            
            if SERVICE.state.get("running"):
                raise ValueError("Идёт legacy-парсинг — дождитесь его окончания")

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
                    "Все тексты — ещё заготовки (в них осталась метка "
                    "«ЗАМЕНИТЕ ЭТОТ ТЕКСТ») — напишите хотя бы один "
                    "или включите предпросмотр"
                )

            if not config.dry_run:
                report = self.preflight()
                if not report["ready"]:
                    titles = [c["title"] for c in report["checks"]
                              if c["blocking"] and not c["ok"]]
                    raise ValueError("Не готово к отправке: " + ", ".join(titles))
                # BROWSER_LOCK рассылка больше НЕ держит весь прогон: сбор и
                # рассылка работают ОДНОВРЕМЕННО — у каждого своя вкладка в
                # общем Chrome (механизм _focus_tab), замок нужен только
                # коротким задачам вроде проверки входящих. Решение оператора
                # 03.10.2026.

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

        from src.outreach.accounts import get_account
        try:
            account = get_account(self.DATA_DIR, getattr(config, "sender_account_id", "") or "main")
        except Exception:
            account = None

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
                account=account,
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
            # Risk Control: отдельно и громко — ограничение платформы требует
            # человека, обычная сводка для этого недостаточна.
            if report.get("risk"):
                risk = report["risk"]
                tg.send(
                    f"⚠️ ОГРАНИЧЕНИЕ ПЛАТФОРМЫ\n"
                    f"Маркер: {risk.get('marker')}\n"
                    f"Этап: {risk.get('stage') or '—'}\n"
                    f"Продавец: {risk.get('seller_id') or '—'}\n"
                    f"Отправка остановлена, инцидент записан. "
                    f"После разбора: /resume или кнопка «Возобновить»."
                )
            HUB.publish({"type": "send", **self.state})
            HUB.publish({"type": "send_done", "stats": self.state})
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Отправка не удалась")
            self._release_browser()
            from src.notify import telegram as tg
            tg.send(f"⚠️ Отправка упала: {str(exc)[:300]}\nЗапусти заново после разбора "
                    f"(частая причина — слетевшая сессия Ozon).")
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


# --- Автономный режим + командный Telegram -------------------------------- #
#
# Раунд автономного режима — это обычный SEND.start(): все его проверки
# (ночной сбор, воркеры, замок браузера, preflight) переиспользуются как есть.
# Супервизор только решает, когда запускать следующий раунд, и переводит
# режим в паузу там, где нужен человек (ограничение платформы).

from src.notify.tgbot import TgPoller, parse_command
from src.outreach.autonomous import AutonomousRunner, Supervisor


def _tg_send(text: str) -> bool:
    from src.notify import telegram as tg
    return tg.send(text)


def _tg_chat_id() -> str:
    from src.notify import telegram as tg
    return tg._credentials()[1]


def _tg_command_handler(text: str) -> str:
    """Команды оператора в Telegram. Отвечает только в настроенный чат."""
    cmd, _arg = parse_command(text)
    store = CRUD.store()
    if cmd == "/status":
        health = store.queue_health()
        auto = AUTONOMOUS.status()
        send_state = SEND.status()
        incident = store.open_risk_incident()
        lines = [
            "🤖 Автономный режим: "
            + ("работает" if auto.get("thread_alive") else "остановлен")
            + (f"\n  пауза: {auto.get('paused_reason')}" if auto.get("paused_reason") else ""),
            f"Раундов: {auto.get('rounds', 0)}, отправлено: {auto.get('sent', 0)}, "
            f"ошибок: {auto.get('failed', 0)}, пропущено: {auto.get('skipped', 0)}",
            f"Очередь: готово {health['ready']}, с ошибкой {health['failed']}",
            f"Сегодня отправлено: {store.sent_today()}",
            "Рассылка: " + ("идёт" if send_state.get("running") else "стоит")
            + (f" — {send_state.get('phase')}" if send_state.get("phase") else ""),
            f"Режим: {'реальная отправка' if not SEND.config().dry_run else 'предпросмотр (dry_run)'}",
        ]
        if incident:
            lines.append(
                f"⚠️ Открыт инцидент #{incident['id']}: {incident['marker']} "
                f"от {incident['created_at']} — /resume после разбора")
        return "\n".join(lines)
    if cmd == "/auto_start":
        try:
            AUTONOMOUS.runner.cancel_graceful_stop()
            AUTONOMOUS.start()
        except ValueError as exc:
            return f"Не запущено: {exc}"
        return "🤖 Автономная обработка запущена"
    if cmd == "/auto_stop":
        AUTONOMOUS.stop()
        return "🛑 Штатная остановка: текущий продавец будет дописан, новых не беру"
    if cmd == "/resume":
        result = AUTONOMOUS.resume(store)
        return ("▶️ Возобновлено: инцидент закрыт, стоп-кран снят"
                if result.get("had_incident") else "▶️ Возобновлено (открытых инцидентов не было)")
    if cmd in ("/help", "/start"):
        return (
            "Команды:\n"
            "/status — состояние автономной обработки и очереди\n"
            "/auto_start — запустить автономную обработку\n"
            "/auto_stop — штатно остановить (дописав текущего)\n"
            "/resume — возобновить после ограничения платформы"
        )
    return "Не понял команду. Список: /help"


TG_POLLER = TgPoller(
    handler=_tg_command_handler,
    allowed_chat_id=_tg_chat_id(),
)


# --- Сторож сессии и Chrome ------------------------------------------------ #
#
# Раз в 15 минут проверяет, жива ли сессия Ozon и отвечает ли Chrome.
# Смерть сессии — самый дорогой сбой: основной прогон мог бы «сжечь» очередь
# ошибками «нет кнопки Написать». Сторож ловит это заранее, один раз
# уведомляет и фиксирует состояние для утреннего отчёта.

class SessionWatchdog:
    INTERVAL_SECONDS = 15 * 60

    def __init__(self):
        self.state = {"last_check": "", "logged_in": None, "alerted": False,
                      "chrome_restarts": 0, "note": ""}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watchdog")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            st = dict(self.state)
            if SEND.state.get("running"):
                st["logged_in"] = True
                st["note"] = "идёт рассылка"
            return st

    def _alert_once(self, text: str) -> None:
        if not self.state.get("alerted"):
            self.state["alerted"] = True
            _tg_send(text)
            HUB.publish({"type": "log", "line": text})

    def _auto_resume_if_due(self) -> None:
        """Снимает стоп-кран, когда истёк срок, названный САМОЙ платформой.

        Это исполнение правила Ozon, а не обход: платформа написала «можно
        после 12:43 UTC» — в этот момент мы разрешаем отправку и сообщаем
        оператору. Срок без until_at (платформа не назвала) не снимается
        никогда — только оператором.
        """
        stop_file = SEND.DATA_DIR / "STOP"
        if not stop_file.exists():
            return
        incident = CRUD.store().open_risk_incident()
        until = str(incident["until_at"] or "") if incident else ""
        if not until:
            return
        try:
            until_dt = datetime.fromisoformat(until)
        except ValueError:
            return
        if datetime.now(timezone.utc) < until_dt:
            return
        AUTONOMOUS.resume(CRUD.store())
        _tg_send("⏰ Срок лимита Ozon истёк (платформа разрешала после "
                 f"{until}). Стоп-кран снят автоматически — можно запускать "
                 "рассылку. Лучше начать с уменьшенным дневным лимитом.")
        HUB.publish({"type": "log", "line": "Сторож: срок лимита истёк, стоп-кран снят"})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._auto_resume_if_due()
                self._check()
            except Exception as exc:                         # noqa: BLE001
                logging.getLogger(__name__).warning(f"Сторож: сбой проверки: {exc}")
            # Спим мелкими шагами, чтобы stop реагировал быстро
            for _ in range(self.INTERVAL_SECONDS):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def _check(self) -> None:
        from src.outreach.sender import build_driver

        # Пока идёт рассылка, вкладку не трогаем: фокус-баттлы ломают ввод.
        if SEND.state.get("running"):
            with self._lock:
                self.state["logged_in"] = True
                self.state["last_check"] = time.strftime("%H:%M")
                self.state["note"] = "идёт рассылка"
            return
        if not BROWSER_LOCK.acquire(blocking=False):
            with self._lock:
                self.state["note"] = "проверка пропущена: браузер занят"
            return
        try:
            from src.crm.risk import load_extra_markers
            from src.outreach.sender import ChatClient, ChatSelectors, raise_if_platform_restricted

            try:
                manager, driver = build_driver(headless=False)
            except Exception as exc:
                # Chrome мёртв: пробуем поднять службу (на VM у yc-user есть
                # sudo на systemctl) и один раз переподключиться.
                if "chrome not reachable" in str(exc).lower():
                    restarted = self._restart_chrome()
                    if restarted:
                        manager, driver = build_driver(headless=False)
                    else:
                        raise
                else:
                    raise
            try:
                selectors = ChatSelectors.load(SEND.DATA_DIR / "selectors.json")
                client = ChatClient(driver, selectors, manager=manager)
                client._focus()
                driver.get("https://www.ozon.ru/")
                time.sleep(3)
                marker_ok = True
                try:
                    raise_if_platform_restricted(
                        driver, load_extra_markers(SEND.DATA_DIR), stage="сторож")
                except Exception:
                    marker_ok = False
                logged_in = marker_ok and bool(client.is_logged_in())
                self._apply(logged_in, marker_ok)
            finally:
                try:
                    manager.close()
                except Exception:                            # noqa: BLE001
                    pass
        finally:
            BROWSER_LOCK.release()

    def _apply(self, logged_in: bool, page_ok: bool) -> None:
        with self._lock:
            self.state["last_check"] = time.strftime("%H:%M")
            self.state["logged_in"] = logged_in
            if not page_ok:
                self._alert_once("⚠️ Сторож: Ozon отдаёт заглушку/антибот. "
                                 "Отправка может остановиться — проверь экран VM.")
                self.state["note"] = "признаки ограничения на странице"
            elif logged_in:
                if self.state.get("alerted"):
                    _tg_send("✅ Сессия Ozon восстановлена — можно продолжать.")
                self.state["alerted"] = False
                self.state["note"] = ""
            else:
                self._alert_once(
                    "⚠️ СЕССИЯ OZON СЛЕТЕЛА. Зайди через noVNC "
                    "(http://localhost:6080/vnc.html, пароль VNC в ~/.x11vnc.pass) "
                    "и авторизуйся — иначе рассылка останавливается.")
                self.state["note"] = "сессия слетела"
                if SEND.state.get("running"):
                    SEND.stop()
                    _tg_send("🛑 Рассылка остановлена сторожем: без сессии продолжать нельзя — "
                             "продавцы получали бы ошибки вместо сообщений.")

    def _restart_chrome(self) -> bool:
        """Перезапускает службу Chrome. На Mac (локальная разработка) тихо нет."""
        try:
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "ozon-chrome"],
                capture_output=True, timeout=30,
            )
            ok = result.returncode == 0
        except Exception:                                    # noqa: BLE001
            ok = False
        if ok:
            with self._lock:
                self.state["chrome_restarts"] = int(self.state.get("chrome_restarts", 0)) + 1
            time.sleep(20)   # Chrome должен поднять отладочный порт
            _tg_send(f"🔄 Chrome на VM падал — служба перезапущена "
                     f"(№{self.state['chrome_restarts']}), проверяю сессию.")
            HUB.publish({"type": "log", "line": "Сторож: Chrome перезапущен"})
        return ok


WATCHDOG = SessionWatchdog()


# --- Ежедневные задания: ночной пересчёт лидов + утренний отчёт ------------ #

def _next_run_at(hour: int, minute: int) -> datetime:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _morning_report_text(store) -> str:
    """Отчёт «как оно живёт» — все цифры из фактов базы, без оценок на глаз."""
    health = store.queue_health()
    comms = store.communication_stats()
    inbox = store.inbox_stats(24)
    sent24 = store.sent_last_24h()
    incident = store.open_risk_incident()
    stages = store.stage_counts()
    total = store.leads_distribution()["total"]
    wd = WATCHDOG.status()
    session = ("✅ ок (проверено " + str(wd.get("last_check") or "—") + ")"
               if wd.get("logged_in") else
               "⚠️ НЕ ПРОВЕРЕНА" if wd.get("logged_in") is None else
               "⚠️ СЛЕТЕЛА — зайди через noVNC")
    lines = [
        "☀️ Отчёт CRM за сутки",
        f"📨 отправлено за 24ч: {sent24} (всего написано: {comms['messaged']})",
        f"💬 ответов за 24ч: {inbox['replies']} (горячих: {inbox['hot']})",
        f"📋 очередь: готово {health['ready']}, ошибок {health['failed']}, база {total}",
        f"🤝 переговоры {stages.get('negotiation', 0)} · сделки {stages.get('deal', 0)} "
        f"· закрыли {stages.get('dead', 0)}",
        f"🔒 сессия Ozon: {session}",
    ]
    if incident:
        until = str(incident["until_at"] or "")
        lines.append("🚨 ОТКРЫТ ИНЦИДЕНТ #" + str(incident["id"]) + ": " + incident["marker"]
                     + (f" — лимит до {until} UTC" if until else "")
                     + " — /resume после разбора")
    else:
        lines.append("🚨 инцидентов нет")
    auto = AUTONOMOUS.status()
    lines.append("🤖 автономный режим: " + ("работает" if auto.get("thread_alive") else "выключен"))
    return "\n".join(lines)


class DailyJobs:
    """Ночные/утренние задачи в одном потоке: пересчёт Lead Score (03:30) и
    отчёт в Telegram (09:00). Время отчёта — OZON_REPORT_TIME, отключение —
    OZON_DAILY_JOBS=0."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run: dict = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daily-jobs")
        self._thread.start()

    def _jobs(self) -> list:
        report_h, report_m = 9, 0
        raw = os.environ.get("OZON_REPORT_TIME", "").strip()
        match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        if match:
            report_h, report_m = int(match.group(1)), int(match.group(2))
        return [("lead_recompute", _next_run_at(3, 30)),
                ("morning_report", _next_run_at(report_h, report_m))]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                jobs = self._jobs()
                soonest_name, soonest_at = min(jobs, key=lambda j: j[1])
                wait = (soonest_at - datetime.now()).total_seconds()
                if wait > 0:
                    self._stop.wait(min(wait, 60))
                    continue
                today = datetime.now().strftime("%Y-%m-%d")
                if self.last_run.get(soonest_name) == today:
                    continue
                self.last_run[soonest_name] = today
                if soonest_name == "lead_recompute":
                    self._run_lead_recompute()
                else:
                    self._run_morning_report()
            except Exception as exc:                         # noqa: BLE001
                logging.getLogger(__name__).warning(f"Ежедневное задание упало: {exc}")
                self._stop.wait(60)

    def _run_lead_recompute(self) -> None:
        from src.crm.leads import LeadConfig

        store = CRUD.store()
        count = store.recompute_all_lead_scores(LeadConfig.load(store.lead_config_path))
        HUB.publish({"type": "log", "line": f"Ночной пересчёт Lead Score: {count} продавцов"})

    def _run_morning_report(self) -> None:
        text = _morning_report_text(CRUD.store())
        _tg_send(text)
        HUB.publish({"type": "log", "line": "Утренний отчёт отправлен в Telegram"})


DAILY_JOBS = DailyJobs()


# --- Фоллоу-ап: вежливое второе касание молчащим ---------------------------- #

class FollowupRunner:
    """Запуск прохода фоллоу-апа в фоне (кнопка «Фоллоу-ап сейчас» и
    автоматический проход перед раундами автономного режима)."""

    def __init__(self):
        self.state = {"running": False, "phase": "", "last_report": {},
                      "started_at": None, "finished_at": None}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def config_path(self) -> Path:
        return SEND.DATA_DIR / "followup.json"

    def config(self):
        from src.outreach.followup import FollowupConfig
        return FollowupConfig.load(self.config_path())

    def save_config(self, data: dict) -> dict:
        from dataclasses import asdict

        config = self.config()
        for key, value in (data or {}).items():
            if hasattr(config, key):
                current = getattr(config, key)
                try:
                    setattr(config, key, type(current)(value))
                except (TypeError, ValueError):
                    raise ValueError(f"Неверное значение для {key}: {value}")
        config.save(self.config_path())
        return asdict(config)

    def settings(self) -> dict:
        from dataclasses import asdict
        return asdict(self.config())

    def status(self) -> dict:
        return dict(self.state)

    def start(self, dry_run: bool = False) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("Фоллоу-ап уже идёт")
            if NIGHT.running():
                raise ValueError("Идёт ночной сбор — он держит браузер")
            if SEND.state.get("running"):
                raise ValueError("Идёт рассылка — фоллоу-ап подождёт её окончания")
            config = self.config()
            if not config.enabled and not dry_run:
                raise ValueError("Фоллоу-апы выключены — включи галочку и сохрани")
            self._stop.clear()
            self.state.update({"running": True, "phase": "Запуск",
                               "started_at": time.time(), "finished_at": None})
        self._thread = threading.Thread(target=self._run,
                                        args=(config, dry_run), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, config, dry_run: bool) -> None:
        from src.outreach.followup import run_followup_round

        def log(message: str) -> None:
            HUB.publish({"type": "log", "line": message})

        try:
            report = run_followup_round(
                CRUD.store(), config, SEND.DATA_DIR,
                sender_account_id=SEND.config().sender_account_id,
                dry_run=dry_run, browser_guard=None if dry_run else BROWSER_LOCK,
                stop_event=self._stop, on_progress=log,
            )
            with self._lock:
                self.state.update({"running": False, "phase": "Готово",
                                   "last_report": report,
                                   "finished_at": time.time()})
            log(f"Фоллоу-ап завершён: отправлено {report.get('sent', 0)}"
                + (f", причина: {report.get('reason')}" if report.get("reason") else ""))
            if report.get("sent"):
                _tg_send(f"📨 Фоллоу-ап: отправлено {report['sent']} повторных касаний")
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Фоллоу-ап не удался")
            with self._lock:
                self.state.update({"running": False, "phase": "Ошибка",
                                   "error": str(exc)[:400], "finished_at": time.time()})
            HUB.publish({"type": "log", "line": f"Ошибка фоллоу-апа: {exc}"})



FOLLOWUP = FollowupRunner()


# Мультиаккаунт выведен из эксплуатации (решение оператора 03.10.2026):
# одна сессия, один стоп-кран, один лимит.



def _autonomous_round() -> None:
    """Раунд автономного режима: сначала фоллоу-ап (если включён), потом
    обычная рассылка. Фоллоу-ап сам берёт замок браузера."""
    from src.outreach.followup import run_followup_round

    try:
        config = FOLLOWUP.config()
        if config.enabled:
            report = run_followup_round(
                CRUD.store(), config, SEND.DATA_DIR,
                sender_account_id=SEND.config().sender_account_id,
                browser_guard=BROWSER_LOCK,
            )
            HUB.publish({"type": "log", "line":
                         f"Фоллоу-ап: отправлено {report.get('sent', 0)}"
                         + (f", причина: {report.get('reason')}" if report.get("reason") else "")})
    except Exception as exc:                                 # noqa: BLE001
        HUB.publish({"type": "log", "line": f"Фоллоу-ап перед раундом не удался: {exc}"})
    SEND.start()


AUTONOMOUS = Supervisor(
    AutonomousRunner(
        data_dir=REPO_ROOT / "outreach_data",
        state_path=REPO_ROOT / "data" / "autonomous_state.json",
        lock_path=REPO_ROOT / "data" / "autonomous.lock",
        stop_path=REPO_ROOT / "data" / "autonomous.stop",
        tg_send=_tg_send,
    ),
    start_round=_autonomous_round,
    round_running=lambda: bool(SEND.state.get("running")),
    round_snapshot=lambda: dict(SEND.state),
)


def _experiment_candidates(store, criteria: dict) -> list:
    """Отбор продавцов в эксперимент по критериям (§11.4).

    Поддерживаются: min_score, max_score, limit. По умолчанию — топ-200
    подтверждённых по score. Критерии сохраняются в experiment_versions,
    поэтому состав группы воспроизводим.
    """
    where = ["verified_by IN ('uscc', 'warehouse')"]
    params: list = []
    if criteria.get("min_score") is not None:
        where.append("score >= ?")
        params.append(float(criteria["min_score"]))
    if criteria.get("max_score") is not None:
        where.append("score <= ?")
        params.append(float(criteria["max_score"]))
    limit = int(criteria.get("limit") or 200)
    with store._connect() as conn:
        rows = conn.execute(
            f"SELECT seller_id FROM sellers WHERE {' AND '.join(where)} "
            "ORDER BY score DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [r["seller_id"] for r in rows]


# --------------------------------------------------------------------------- #
#  Шина событий: лог и статус уходят подписчикам SSE
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=500)
        self.log_dir = REPO_ROOT / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

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
            # Перенос логов в папку logs/
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                log_file = self.log_dir / f"crm_live_{today}.log"
                line = event.get("line", "")
                level = event.get("level", "INFO")
                ts_val = event.get("ts", time.time())
                ts_str = datetime.fromtimestamp(ts_val).strftime("%H:%M:%S")
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{ts_str}] [{level}] {line}\n")
            except Exception:
                pass
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
            # Симметрично остальным задачам: legacy-парсер тоже держит общий
            # браузер и не должен стартовать поверх ночных/воркерских проходов
            if NIGHT.running():
                raise ValueError("Идёт ночной сбор — он держит браузер")
            if WORKERS.running("ozon"):
                raise ValueError("Работает воркер Ozon — дождитесь его завершения")
            if HARVEST.status().get("running"):
                raise ValueError("Идёт обычный сбор — дождитесь его окончания")

            # Тот же замок, что у сбора: браузер один на всех
            if not BROWSER_LOCK.acquire(blocking=False):
                raise ValueError("Браузер занят другой задачей — дождитесь её завершения")
            self._holds_browser = True
            # Замок берём до опасного участка, но get_manager/start_parsing
            # могут бросить исключение — без finally замок оставался бы
            # удержанным навсегда, и все браузерные задачи отказывали до
            # перезапуска дашборда (находка аудита, воспроизведённая вживую)
            try:
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
            except BaseException:
                self._release_browser()
                raise
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Клиент отключился до чтения ответа: фоновые скрипты проверяют
            # занятость перед каждым шагом и закрывают соединение сразу после
            # отправки запроса. Запрос обработан, ответ доставить некому —
            # это не ошибка сервера, и трейсбеками её не лечат.
            logging.getLogger(__name__).debug("Клиент отключился до получения ответа")

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

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    # --- GET ---------------------------------------------------------------- #
    def do_GET(self):
        try:
            self._do_get()
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).exception("Ошибка обработки GET %s", self.path)
            self._send_json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def _do_get(self):
        # Авто-редирект с небезопасного HTTP на защищённый HTTPS
        if self.headers.get("X-Forwarded-Proto") == "http":
            host = self.headers.get("Host", "crm.sh-coding.ru")
            self.send_response(301)
            self.send_header("Location", f"https://{host}{self.path}")
            self.end_headers()
            return

        path = urlparse(self.path).path
        if path in PUBLIC_STATIC:
            name, content_type = PUBLIC_STATIC[path]
            return self._send_file(WEB_DIR / name, content_type)
        # Панель Lead Intelligence (§11.9): отдельная страница за той же
        # авторизацией, данные берёт из /api/analytics и /api/leads/top.
        if path in ("/leads", "/leads.html") and self._check_auth():
            return self._send_file(WEB_DIR / "leads.html", "text/html; charset=utf-8")
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
                # Нишевые категории: где больше продавцов и кто отвечает
                "niche_stats": store.category_stats(limit=10),
            })
        if path == "/api/seller/detail":
            seller_id = self._query().get("seller_id", "")
            detail = self._crm(self._query()).detail(seller_id)
            return self._send_json(detail or {"error": "Продавец не найден"},
                                   200 if detail else 404)
        if path == "/api/sellers/export.csv":
            marketplace = str(self._query().get("marketplace", "ozon"))
            # Инъекция заголовков: имя файла собирается из query-параметра,
            # поэтому пропускаем только безопасные символы (находка аудита)
            if not re.fullmatch(r"[a-z0-9_-]{1,20}", marketplace):
                return self._send_json({"error": "marketplace: ozon|wb"}, 400)
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

        if path == "/api/sellers/backup.sqlite3":
            if not DB_PATH.exists():
                return self._send_json({"error": "База не найдена"}, 404)
            db_bytes = DB_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-sqlite3")
            self.send_header("Content-Disposition", 'attachment; filename="sellers.sqlite3"')
            self.send_header("Content-Length", str(len(db_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(db_bytes)
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
                "cap_today": SEND.cap_today(),
            })
        if path == "/api/autonomous":
            # Состояние автономного режима одним куском: супервизор, очередь,
            # открытый инцидент риска. Только чтение — управляющие вызовы POST.
            store = CRUD.store()
            incident = store.open_risk_incident()
            return self._send_json({
                "autonomous": AUTONOMOUS.status(),
                "send": SEND.status(),
                "queue": store.queue_health(),
                "risk_stop": AUTONOMOUS.runner.risk_stop_active(),
                "incident": dict(incident) if incident else None,
            })
        if path == "/api/analytics":
            # §11.9: все показатели считаются SQL-агрегатами по фактам базы.
            store = CRUD.store()
            return self._send_json({
                "base": store.leads_distribution(),
                "comms": store.communication_stats(),
                "funnel": store.funnel_counts(),
                "transitions": store.funnel_transitions(30),
            })
        if path == "/api/leads/top":
            # Верх очереди Lead Intelligence: cached scores, порядок = priority
            store = CRUD.store()
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT seller_id, shop_name, orders, reviews, months_on_ozon, "
                    "rating, product_count, province, score, status, lead_grade, "
                    "grade_reason, commercial_score, engagement_score, priority, "
                    "next_task, next_task_at "
                    "FROM sellers WHERE verified_by IN ('uscc', 'warehouse') "
                    "ORDER BY commercial_score DESC, score DESC LIMIT ?",
                    (int(self._query().get("limit", 50)),),
                ).fetchall()
            return self._send_json({"rows": [dict(r) for r in rows]})
        if path == "/api/followup":
            return self._send_json({"settings": FOLLOWUP.settings(),
                                    "state": FOLLOWUP.status()})
        if path == "/api/watchdog":
            return self._send_json(WATCHDOG.status())
        if path == "/api/experiments":
            store = CRUD.store()
            experiment_id = self._query().get("id", "")
            if experiment_id:
                return self._send_json(store.experiment_report(int(experiment_id)))
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT id, name, status, created_at, finished_at, variants "
                    "FROM experiments ORDER BY id DESC").fetchall()
            return self._send_json({"experiments": [dict(r) for r in rows]})
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
                # Ответ УЖЕ отправлен (303 + cookie). Раньше здесь был ещё
                # return self._send_json({"ok": True}) — второй ответ в то же
                # соединение: BrokenPipe на сервере и рассинхрон keep-alive
                # у клиента (находка аудита, подтверждена живым дампом).
                return
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
                result = self._crm_for_body(data).set_status(seller_id, status)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            # Событие в SSE: отметка менеджера видна всем открытым дашбордам
            # (и с компьютера, и с телефона) мгновенно, без перезагрузки.
            HUB.publish({"type": "seller_update",
                         "seller_id": seller_id, "status": status,
                         "marketplace": str(data.get("marketplace", "ozon"))})
            return self._send_json(result)

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
            if NIGHT.running():
                BROWSER_LOCK.release()
                return self._send_json(
                    {"error": "Идёт ночной сбор — он держит браузер. Остановите его или дождитесь финиша"}, 409)
            try:
                from src.crm.reply import reply_to_seller

                result = reply_to_seller(SEND.DATA_DIR, CRUD.store(), seller_id, text)
                HUB.log(f"Ответ из дашборда: {seller_id} — текст ушёл в чат Ozon")
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": str(exc)[:300]}, 500)
            finally:
                BROWSER_LOCK.release()
            # Telegram — ПОСЛЕ ответа клиенту и вне защищаемого блока: падение
            # уведомления не должно возвращать 500 — сообщение-то УШЛО, и
            # человек отправил бы повтор (дубликат продавцу). Находка аудита.
            try:
                from src.notify import telegram as tg

                shop = (CRUD.detail(seller_id) or {}).get("shop_name") or seller_id
                tg.send(f"💬 Ответ ушёл продавцу {shop}:\n{text[:400]}")
            except Exception:                             # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Telegram-уведомление об ответе не отправилось", exc_info=True)
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
            # Legacy in-process сбор.
            try:
                delay = data.get("delay")
                harvest_url = str(data.get("url", "")).strip()
                pages = int(data.get("pages") or 10)
                HARVEST.start(
                    harvest_url,
                    pages,
                    delay=float(delay) if delay is not None else None,
                    loop=bool(data.get("loop")),
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

        if path == "/api/autonomous/start":
            try:
                AUTONOMOUS.runner.cancel_graceful_stop()
                AUTONOMOUS.start()
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 409)
            return self._send_json({"ok": True, "autonomous": AUTONOMOUS.status()})

        if path == "/api/autonomous/stop":
            AUTONOMOUS.stop()
            return self._send_json({"ok": True})

        if path == "/api/autonomous/resume":
            # Только оператор снимает риск-паузу: инцидент закрывается, стоп-кран
            # удаляется, следующий раунд уходит сам.
            return self._send_json({"ok": True, **AUTONOMOUS.resume(CRUD.store())})

        if path == "/api/seller/priority":
            # Ручной приоритет очереди: выше priority — раньше уйдёт (не меняя
            # формулу score). Порядок: priority DESC, score DESC.
            seller_id = str(data.get("seller_id", "")).strip()
            try:
                priority = int(data.get("priority", 0))
            except (TypeError, ValueError):
                return self._send_json({"error": "priority должен быть числом"}, 400)
            if not seller_id:
                return self._send_json({"error": "нужен seller_id"}, 400)
            changed = CRUD.store().set_priority(seller_id, priority)
            return self._send_json(
                {"ok": changed, "seller_id": seller_id, "priority": priority},
                200 if changed else 404,
            )

        if path == "/api/seller/grade":
            # §11.3: ручная категория лида с обязательной причиной.
            store = CRUD.store()
            try:
                changed = store.set_lead_grade(
                    str(data.get("seller_id", "")),
                    str(data.get("grade", "")),
                    str(data.get("reason", "")),
                )
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": changed}, 200 if changed else 404)

        if path == "/api/seller/next-task":
            # §11.1: следующая запланированная задача менеджера.
            changed = CRUD.store().set_next_task(
                str(data.get("seller_id", "")),
                str(data.get("task", "")),
                str(data.get("when", "")),
            )
            return self._send_json({"ok": changed}, 200 if changed else 404)


        if path == "/api/leads/stage":
            seller_id = str(data.get("seller_id", "")).strip()
            stage = str(data.get("stage", "")).strip()
            reason = str(data.get("reason", "")).strip()
            if not seller_id or not stage:
                return self._send_json({"error": "нужны seller_id и stage"}, 400)
            store = CRUD.store()
            changed = store.set_stage(seller_id, stage, reason, "manager")
            return self._send_json({"ok": changed})

        if path == "/api/leads/recompute":
            # Пересчёт Lead Intelligence: после правки lead_config.json или
            # массового обновления категорий.
            store = CRUD.store()
            from src.crm.leads import LeadConfig
            config = LeadConfig.load(store.lead_config_path)
            count = store.recompute_all_lead_scores(
                config, limit=int(data.get("limit") or 0))
            return self._send_json({"recomputed": count})

        if path == "/api/experiments/create":
            # §11.4: эксперимент с воспроизводимыми условиями — критерии и
            # варианты сохраняются версией в базе, назначение детерминировано.
            store = CRUD.store()
            name = str(data.get("name", "")).strip()
            criteria = data.get("criteria") or {}
            variants = [str(v).strip() for v in (data.get("variants") or []) if str(v).strip()]
            if not name or not variants:
                return self._send_json(
                    {"error": "нужны name и хотя бы один вариант (имя шаблона)"}, 400)
            try:
                experiment_id = store.create_experiment(name, criteria, variants)
                assigned = store.assign_experiment_sellers(
                    experiment_id, _experiment_candidates(store, criteria), variants)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "experiment_id": experiment_id,
                                    "assigned": assigned})

        if path == "/api/experiments/finish":
            ok = CRUD.store().finish_experiment(int(data.get("id") or 0))
            return self._send_json({"ok": ok}, 200 if ok else 404)

        if path == "/api/followup/settings":
            try:
                return self._send_json({"settings": FOLLOWUP.save_config(data)})
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)

        if path == "/api/followup/run":
            try:
                FOLLOWUP.start(dry_run=bool(data.get("dry_run")))
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 409)
            return self._send_json({"ok": True})

        if path == "/api/followup/stop":
            FOLLOWUP.stop()
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
            if SEND.state.get("running"):
                WATCHDOG._apply(True, True)
                return self._send_json({
                    "logged_in": True,
                    "note": "рассылка активна — сессия подтверждена",
                    "last_check": "только что"
                })
            from src.outreach.sender import check_login_live
            live = check_login_live(SEND.DATA_DIR, on_progress=HUB.log)
            is_logged = bool(live.get("logged_in"))
            WATCHDOG._apply(is_logged, True)
            return self._send_json(live)

        if path == "/api/inbox/check":
            # Проверка входящих открывает вкладку в общем Chrome: как и сбор
            # с отправкой, держит замок, чтобы задачи не мешали друг другу.
            if NIGHT.running():
                return self._send_json(
                    {"error": "Идёт ночной сбор — он держит браузер. Проверка подождёт"}, 409)
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
                # Горячие лиды — сразу в Telegram; остальные ответы — в статистику
                from src.notify import telegram as tg

                for marked in result["marked"]:
                    hot = marked.get("category") in ("interested", "positive", "needs_info", "negotiation")
                    if hot:
                        tg.send("🔥 ГОРЯЧИЙ ЛИД: " + marked["shop"]
                                + "\nКатегория: " + marked.get("category_label", "")
                                + "\n" + marked["text"][:400])
                    # Негорячие (отказ, нейтральное) — без пуша: в журнале,
                    # статистике и утреннем отчёте они и так есть.
            except Exception as exc:                      # noqa: BLE001
                return self._send_json({"error": f"Не удалось проверить: {exc}"}, 500)
            finally:
                BROWSER_LOCK.release()
            return self._send_json(result)

        if path == "/api/send/template":
            result = SEND.save_template(str(data.get("body", "")))
            code = 200 if result.get("ok") else 400
            return self._send_json(result, code)

        if path == "/api/send/templates/import":
            result = SEND.import_templates(str(data.get("text", "")),
                                           str(data.get("mode", "replace")))
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

    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(log_dir / f"dashboard_{today}.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), HubLogHandler(), file_handler],
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
    # Сторож сессии и ежедневные задания: на локальной машине можно отключить
    # (OZON_SESSION_WATCHDOG=0 / OZON_DAILY_JOBS=0) — они открывают браузер.
    raw_watchdog = (os.environ.get("OZON_SESSION_WATCHDOG") or "").strip().lower()
    if raw_watchdog in ("0", "false", "no", "off"):
        logging.getLogger(__name__).info("Сторож сессии отключён (OZON_SESSION_WATCHDOG=0)")
    else:
        WATCHDOG.start()
        HUB.log("Сторож сессии включён: проверка раз в 15 минут")
    raw_jobs = (os.environ.get("OZON_DAILY_JOBS") or "").strip().lower()
    if raw_jobs in ("0", "false", "no", "off"):
        logging.getLogger(__name__).info("Ежедневные задания отключены (OZON_DAILY_JOBS=0)")
    else:
        DAILY_JOBS.start()
    resumed = resume_night_after_restart()
    if resumed:
        # В HUB попадёт через HubLogHandler: прямой вызов продублировал бы строку
        logging.getLogger(__name__).info(resumed)
    # Командный Telegram: запускаем, только если настроен токен и чат.
    # Поллер один на процесс — два getUpdates воровали бы апдейты друг у друга.
    from src.notify import telegram as _tg

    _token, _chat_id = _tg._credentials()
    if _token and _chat_id:
        TG_POLLER.start()
        HUB.log("Telegram-команды включены (/help в чате бота)")
    # Автономный режим переживает перезапуск дашборда: стейт говорит
    # «running», стоп-крана риска нет — продолжаем сами (как ночной сбор).
    if AUTONOMOUS.status().get("running") and not AUTONOMOUS.runner.risk_stop_active():
        try:
            AUTONOMOUS.start()
            HUB.log("Автономная обработка продолжена после перезапуска")
        except Exception as exc:                             # noqa: BLE001
            logging.getLogger(__name__).warning(f"Автономный режим не продолжился: {exc}")
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