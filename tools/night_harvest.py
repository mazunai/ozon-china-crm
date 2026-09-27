#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ночной автосбор продавцов Ozon: идёт чанками, сам продолжает после сбоев.

Зачем: большой обход раздела «Товары из Китая» за один запуск — это часы
подряд, и любой сбой на середине обнулял прогресс. Здесь сбор идёт порциями
(--chunk-pages страниц), между порциями пауза, а пройденная страница хранится
в data/night_harvest_state.json — после ошибки работа продолжается с того же
места, а не с начала.

Запускается кнопкой из дашборда (NIGHT.start передаёт --url и параметры)
и работает до --until.

    # проверить: один чанк по 2 страницы
    python tools/night_harvest.py --max-chunks 1 --chunk-pages 2 --quiet-pause

    # ночной режим вручную (до 08:00 по времени ВМ)
    python tools/night_harvest.py --until 08:00

Остановить: коснуться data/night_harvest.stop (или systemctl stop
parser-night-harvest). Текущий чанк доиграет, новые не начнутся.
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

DATA_DIR = REPO_ROOT / "data"
STATE_PATH = DATA_DIR / "night_harvest_state.json"
STOP_PATH = DATA_DIR / "night_harvest.stop"
LOCK_PATH = DATA_DIR / "night_harvest.lock"
LOG_DIR = REPO_ROOT / "logs"

DEFAULT_SECTION = "https://www.ozon.ru/highlight/tovary-iz-kitaya-935133/"

# Куда складывать прогресс и лог — общие для всех запусков, поэтому и в модуле.
_stopping = False


def log(message: str, log_file: Path) -> None:
    """Строка с временем и в stdout (journald), и в файл ночи."""
    write_raw(f"[{datetime.now().strftime('%d.%m %H:%M:%S')}] {message}", log_file)


def write_raw(line: str, log_file: Path) -> None:
    """Готовая строка — как есть: вывод сборщика уже со своими датами."""
    print(line, flush=True)
    try:
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def already_running() -> int:
    """Возвращает pid живого запуска или 0.

    Два ночных сбора одновременно подерутся за один Chrome и перепутают
    вкладки, поэтому второй запуск просто выходит.
    """
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


def env_credentials() -> tuple:
    """Логин и пароль дашборда из окружения или из .env."""
    user = os.environ.get("OZON_DASHBOARD_USER", "")
    password = os.environ.get("OZON_DASHBOARD_PASSWORD", "")
    if user and password:
        return user, password
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return user, password
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key == "OZON_DASHBOARD_USER":
            user = value
        elif key == "OZON_DASHBOARD_PASSWORD":
            password = value
    return user, password


def dashboard_json(base_url: str, path: str, opener) -> dict:
    try:
        with opener.open(base_url.rstrip("/") + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def dashboard_busy(base_url: str) -> str:
    """Занят ли браузер дашбордом: 'сбор', 'рассылка' или ''.

    Сбор из скрипта и задачи из дашборда делят один Chrome с одной сессией
    Ozon. Если дашборд сейчас что-то делает, ждём — иначе вкладки перепутаются
    и данные поедут. Не смогли спросить (нет пароля, дашборд лежит) — считаем,
    что свободно, и работаем: ночью дашбордом обычно не пользуются.
    """
    user, password = env_credentials()
    if not password:
        return ""
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"username": user, "password": password}).encode("utf-8")
    try:
        opener.open(base_url.rstrip("/") + "/api/login", data=data, timeout=10).read()
    except Exception:                                       # noqa: BLE001
        return ""
    if dashboard_json(base_url, "/api/harvest", opener).get("running"):
        return "сбор"
    # Ретро-проход категорий гоняет тот же Chrome вне дашборда (аудит)
    if (DATA_DIR / "category_backfill.lock").exists():
        return "ретро-проход категорий"
    send = dashboard_json(base_url, "/api/send", opener)
    if (send.get("state") or {}).get("running"):
        return "рассылка"
    return ""


def run_chunk(args, page: int, log_file: Path) -> tuple:
    """Один чанк сбора. Возвращает (итог или None, признак ошибки).

    Вывод сборщика читаем построчно и сразу пишем в лог: по нему видно, что
    обход живёт (в дашборде это хвост лога, он обновляется по таймеру).
    Раньше вывод копился в памяти и появлялся только в конце чанка — при
    проходе в сотню страниц это выглядело как «сбор не работает».
    """
    command = [
        PYTHON, str(REPO_ROOT / "run_harvest.py"), "harvest",
        "--url", args.url,
        "--pages", str(args.chunk_pages),
        "--start-page", str(page),
        "--delay", str(args.delay),
        "--refresh-days", str(args.refresh_days),
        "--json",
    ]
    log(f"чанк: страницы {page}–{page + args.chunk_pages - 1}", log_file)
    started = time.time()
    try:
        process = subprocess.Popen(
            command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as exc:
        log(f"сбор не запустился: {exc}", log_file)
        return None, True

    stats = None
    deadline = started + args.chunk_timeout
    previous = ""
    for line in process.stdout:
        line = line.rstrip()
        if not line or line == previous:
            # Одна и та же строка приходит и в stdout, и через logging —
            # второй раз её в лог не пишем
            continue
        previous = line
        if line.startswith("HARVEST_JSON "):
            try:
                stats = json.loads(line[len("HARVEST_JSON "):])
            except ValueError:
                stats = None
            continue
        write_raw(line, log_file)
        if time.time() > deadline:
            process.kill()
            log(f"чанк не уложился в {args.chunk_timeout} с — прерываю его", log_file)
            return None, True
    process.wait()

    took = int(time.time() - started)
    if stats is None:
        log(f"чанк упал за {took} с, код {process.returncode}", log_file)
        return None, True
    log(
        "  итог: страниц {pages}, товаров {products}, продавцов {sellers} "
        "(USCC {uscc}, склад {warehouse}, отсеяно {rejected}), ошибок {errors}, {took} с".format(
            took=took, **stats),
        log_file,
    )
    return stats, False


def stop_time(until: str, now: datetime) -> datetime:
    """Ближайшее наступление --until (ночь переходит через полночь)."""
    hour, minute = (int(part) for part in until.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while moment <= now:
        moment += timedelta(days=1)
    return moment


def install_signals() -> None:
    def handler(signum, _frame):
        global _stopping
        _stopping = True
        print(f"получен сигнал {signum} — завершаю после текущего чанка", flush=True)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ночной автосбор продавцов: чанками, с повторами и докаткой",
    )
    parser.add_argument("--url", default=DEFAULT_SECTION, help="раздел Ozon Global")
    parser.add_argument("--until", default="08:00", metavar="ЧЧ:ММ",
                        help="до какого времени собирать (по умолчанию 08:00)")
    parser.add_argument("--max-hours", type=float, default=10.0,
                        help="страховка от случайного запуска днём, часов (по умолчанию 10)")
    parser.add_argument("--chunk-pages", type=int, default=10,
                        help="сколько страниц за один проход (по умолчанию 10)")
    parser.add_argument("--delay", type=float, default=1.2,
                        help="пауза между запросами внутри чанка, сек (по умолчанию 1.2)")
    parser.add_argument("--refresh-days", type=int, default=7,
                        help="перечитывать известных продавцов раз в N дней (0 — никогда)")
    parser.add_argument("--pause", type=float, default=180,
                        help="пауза между чанками, сек (по умолчанию 180)")
    parser.add_argument("--error-pause", type=float, default=600,
                        help="пауза после сбоя чанка, сек (по умолчанию 600)")
    parser.add_argument("--fail-limit", type=int, default=3,
                        help="сбоев подряд до длинной паузы (по умолчанию 3)")
    parser.add_argument("--long-pause", type=float, default=1800,
                        help="длинная пауза после серии сбоев, сек (по умолчанию 1800)")
    parser.add_argument("--chunk-timeout", type=int, default=3600,
                        help="предел на один чанк, сек (по умолчанию 3600)")
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="остановиться после N чанков (0 — без предела; для проверки)")
    parser.add_argument("--start-page", type=int, default=0,
                        help="принудительно начать со страницы N (0 — продолжить с сохранённой)")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8000",
                        help="адрес дашборда для проверки занятости браузера")
    parser.add_argument("--quiet-pause", action="store_true",
                        help="короткие паузы для проверки (5 секунд)")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    busy_pid = already_running()
    if busy_pid:
        print(f"Ночной сбор уже идёт (pid {busy_pid}) — второй запуск не нужен")
        return 0
    take_lock()
    install_signals()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"night_harvest_{datetime.now().strftime('%Y-%m-%d')}.log"

    state = read_state()
    page = args.start_page or int(state.get("next_page") or 1)
    if args.quiet_pause:
        args.pause = args.error_pause = 5
    deadline = min(stop_time(args.until, datetime.now()),
                   datetime.now() + timedelta(hours=args.max_hours))

    log(
        f"старт: начинаю со страницы {page}, чанк {args.chunk_pages} страниц, "
        f"работаю до {deadline.strftime('%d.%m %H:%M')}",
        log_file,
    )

    chunks = failures = 0
    totals = {"products": 0, "sellers": 0, "uscc": 0, "warehouse": 0, "errors": 0}
    try:
        while True:
            if _stopping:
                log("остановка по сигналу", log_file)
                break
            if STOP_PATH.exists():
                log("найден стоп-файл — завершаю", log_file)
                break
            if datetime.now() >= deadline:
                log("время вышло — завершаю", log_file)
                break
            if args.max_chunks and chunks >= args.max_chunks:
                log(f"достигнут предел в {args.max_chunks} чанк(ов) — завершаю", log_file)
                break

            busy = dashboard_busy(args.dashboard)
            if busy:
                log(f"браузер занят ({busy} в дашборде) — жду {int(args.error_pause)} с", log_file)
                time.sleep(args.error_pause)
                continue

            stats, failed = run_chunk(args, page, log_file)
            chunks += 1

            if failed:
                failures += 1
                # Страницу не сдвигаем: следующий заход повторит этот же кусок.
                write_state({"next_page": page, "updated_at": datetime.now().isoformat(timespec="seconds"),
                             "last_result": "error", "failures": failures})
                if failures >= args.fail_limit:
                    log(f"сбоев подряд {failures} — длинная пауза {int(args.long_pause)} с", log_file)
                    failures = 0
                    wait = args.long_pause
                else:
                    log(f"повторю эту же страницу через {int(args.error_pause)} с", log_file)
                    wait = args.error_pause
                time.sleep(wait)
                continue

            failures = 0
            for key in totals:
                totals[key] += int(stats.get(key) or 0)
            chunks_done = chunks

            if int(stats.get("products") or 0) == 0:
                # Товаров на этих страницах нет — раздел кончился, начинаем
                # круг заново: за ночь база обновится и подчистятся метрики.
                page = 1
                log("страницы кончились — начинаю раздел сначала", log_file)
                wait = args.pause * 3
            else:
                page += args.chunk_pages
                wait = args.pause

            write_state({
                "next_page": page,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "last_result": "ok",
                "chunks_done": chunks_done,
                "totals": totals,
            })
            log(
                f"за ночь: чанков {chunks_done}, товаров {totals['products']}, "
                f"продавцов {totals['sellers']} (USCC {totals['uscc']}), "
                f"следующая страница {page}, пауза {int(wait)} с",
                log_file,
            )
            # Небольшой разброс, чтобы сбор не шёл строго по секундомеру
            time.sleep(wait * random.uniform(0.8, 1.2))
    finally:
        write_state({**read_state(), "next_page": page,
                     "updated_at": datetime.now().isoformat(timespec="seconds")})
        release_lock()
        log(
            f"финиш: чанков {chunks}, товаров {totals['products']}, "
            f"продавцов {totals['sellers']}, следующая страница {page}",
            log_file,
        )
        # Итог ночи в Telegram (если настроен токен и chat_id)
        try:
            from src.notify.telegram import send as tg_send

            tg_send(
                "🌙 Ночной сбор завершён: проходов {c}, товаров {p}, продавцов {s} "
                "(USCC {u}), следующая страница {np}".format(
                    c=chunks, p=totals["products"], s=totals["sellers"],
                    u=totals["uscc"], np=page)
            )
        except Exception:                                    # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
