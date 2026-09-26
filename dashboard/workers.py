"""Супервайзер воркеров сбора: запуск, остановка, авто-рестарт, статус.

Каждый маркетплейс — отдельный OS-процесс (run_worker.py), общение по файлам
(src/workers/runtime.py): pid, стейт-JSON, стоп-файл. Изоляция — главное
требование владельца: падение WB не должно задевать работающий Ozon, поэтому
никаких общих ресурсов между воркерами нет — только общий дашборд и общая
машина.

Авто-рестарт: если воркер, который пользователь хотел запустить, умер сам
(крэш, kill -9), супервайзер поднимает его с теми же аргументами. Три
подряд неудачных запуска — сдаться и сказать в лог: бесконечный рестарт
упавшего процесса только жжёт IP маркетплейса.
"""

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.workers.runtime import (                      # noqa: E402
    MARKETPLACES,
    pid_alive,
    pid_path,
    read_state,
    request_stop,
    stop_path,
)

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_CRASHES = 3
# Воркер, проживавший дольше этого, считается стабильно работающим: счётчик
# крэшей обнуляется. Иначе серия давних падений навсегда заблокирует запуск.
STABLE_AFTER_SECONDS = 120
WATCHDOG_INTERVAL = 3.0


class WorkerSupervisor:
    """Управляет процессами run_worker.py по одному на маркетплейс."""

    def __init__(self, repo_root: Path, publish=None):
        self.repo_root = repo_root
        self.script = repo_root / "run_worker.py"
        self.logs_dir = repo_root / "logs"
        # publish — колбэк в SSE-шину дашборда (HUB.publish): воркер-процессу
        # некуда писать события самому, файловый стейт — его единственный язык.
        self._publish = publish or (lambda event: None)
        self._lock = threading.Lock()
        # marketplace → аргументы запуска (пользовательский «замысел»).
        # None = запускать не надо (остановлен или никогда не запускался).
        self._desired: dict = {}
        self._procs: dict = {}
        self._crashes: dict = {mp: 0 for mp in MARKETPLACES}
        self._started_at: dict = {}
        self._auto_restarts: dict = {mp: 0 for mp in MARKETPLACES}
        self._log_offset: dict = {}    # marketplace → сколько байт лога уже ушло в SSE
        self._log_date: dict = {}      # marketplace → дата файла лога (роллится в полночь)
        self._watchdog: threading.Thread | None = None

    # --- Запуск / остановка ------------------------------------------------

    def start(self, marketplace: str, params: dict) -> None:
        if marketplace not in MARKETPLACES:
            raise ValueError(f"Неизвестный маркетплейс: {marketplace}")
        if self.running(marketplace):
            raise ValueError(f"Воркер {marketplace} уже работает")
        if not self.script.exists():
            raise ValueError(f"Нет скрипта воркера: {self.script}")
        self._spawn(marketplace, params)
        with self._lock:
            self._desired[marketplace] = dict(params)
            self._crashes[marketplace] = 0
            self._auto_restarts[marketplace] = 0
        self._ensure_watchdog()

    def _spawn(self, marketplace: str, params: dict) -> None:
        command = [
            sys.executable, str(self.script),
            "--marketplace", marketplace,
            "--pages", str(int(params.get("pages") or 10)),
            "--refresh-days", str(int(params.get("refresh_days") or 7)),
        ]
        url = str(params.get("url") or "").strip()
        if url:
            command += ["--url", url]
        start_page = int(params.get("start_page") or 1)
        if start_page > 1:
            command += ["--start-page", str(start_page)]
        if params.get("delay") is not None:
            command += ["--delay", str(params["delay"])]

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / (
            f"worker_{marketplace}_{datetime.now():%Y-%m-%d}.log"
        )
        with log_path.open("a", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                command, cwd=str(self.repo_root),
                stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        with self._lock:
            self._procs[marketplace] = proc
            self._started_at[marketplace] = time.time()
            self._log_offset[marketplace] = 0
        logger.info(f"Воркер {marketplace} запущен: pid {proc.pid}")

    def stop(self, marketplace: str) -> bool:
        """Просит воркера остановиться стоп-файлом (мягко, доиграет шаг)."""
        stopped = request_stop(marketplace)
        with self._lock:
            self._desired[marketplace] = None      # вотчдог не станет поднимать
        if stopped:
            self._publish({
                "type": "log",
                "line": f"Воркер {marketplace}: запрошена остановка",
            })
        return stopped

    # --- Статус -------------------------------------------------------------

    def running(self, marketplace: str) -> bool:
        proc = self._procs.get(marketplace)
        if proc is not None and proc.poll() is None:
            return True
        # Процесс мог остаться от прошлого запуска дашборда: pid-файл правдивее.
        return pid_alive(marketplace)

    def _tail(self, marketplace: str, lines: int = 15) -> list:
        """Хвост сегодняшнего лога воркера — как у ночного сбора."""
        log_file = self.logs_dir / (
            f"worker_{marketplace}_{datetime.now():%Y-%m-%d}.log"
        )
        if not log_file.exists():
            return []
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()][-lines:]

    def status(self, marketplace: str) -> dict:
        state = read_state(marketplace) or {}
        running = self.running(marketplace)
        return {
            "marketplace": marketplace,
            "running": running,
            "desired": self._desired.get(marketplace) is not None,
            "stop_requested": stop_path(marketplace).exists(),
            "pid": state.get("pid"),
            "state": state,
            "log": self._tail(marketplace),
            "auto_restarts": self._auto_restarts.get(marketplace, 0),
        }

    # --- Вотчдог -------------------------------------------------------------

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(
            target=self._loop, name="worker-watchdog", daemon=True
        )
        self._watchdog.start()

    def _loop(self) -> None:
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            for marketplace in MARKETPLACES:
                try:
                    self._check(marketplace)
                    self._stream_changes(marketplace)
                except Exception:                        # noqa: BLE001
                    logger.exception(f"Вотчдог воркера {marketplace} споткнулся")

    def _check(self, marketplace: str) -> None:
        proc = self._procs.get(marketplace)
        if proc is None or proc.poll() is None:
            return                                       # жив или не нами запущен

        code = proc.poll()
        with self._lock:
            self._procs.pop(marketplace, None)
        desired = self._desired.get(marketplace)

        if desired is None:
            return                                       # остановили намеренно
        if code == 0:
            # Воркер доработал сам (страницы кончились) — замысел исполнен.
            self._desired[marketplace] = None
            self._publish({"type": "worker", "marketplace": marketplace,
                           "running": False, "finished": True})
            return

        lived = time.time() - self._started_at.get(marketplace, time.time())
        if lived > STABLE_AFTER_SECONDS:
            self._crashes[marketplace] = 0
        self._crashes[marketplace] = self._crashes.get(marketplace, 0) + 1

        if self._crashes[marketplace] > MAX_CONSECUTIVE_CRASHES:
            self._desired[marketplace] = None
            self._publish({
                "type": "log",
                "line": f"Воркер {marketplace} падает {self._crashes[marketplace]}-й раз "
                        f"подряд — авто-рестарт выключен, смотрите лог",
            })
            return

        logger.warning(
            f"Воркер {marketplace} умер (код {code}) — перезапускаю "
            f"({self._crashes[marketplace]}/{MAX_CONSECUTIVE_CRASHES})"
        )
        self._auto_restarts[marketplace] = self._auto_restarts.get(marketplace, 0) + 1
        self._spawn(marketplace, desired)
        self._publish({
            "type": "log",
            "line": f"Воркер {marketplace} упал (код {code}) и перезапущен автоматически",
        })

    def _stream_changes(self, marketplace: str) -> None:
        """Новые строки лога воркера → SSE дашборда (живой хвост на экране)."""
        log_file = self.logs_dir / (
            f"worker_{marketplace}_{datetime.now():%Y-%m-%d}.log"
        )
        if not log_file.exists():
            return
        try:
            size = log_file.stat().st_size
        except OSError:
            return
        offset = self._log_offset.get(marketplace, 0)
        if size <= offset:
            self._log_offset[marketplace] = size
            return
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                fresh = handle.read()
        except OSError:
            return
        self._log_offset[marketplace] = size
        lines = [line for line in fresh.splitlines() if line.strip()]
        for line in lines[-30:]:
            # Формат строк — logging: «время - логгер - LEVEL - сообщение».
            message = line.split(" - ", 3)[-1]
            self._publish({
                "type": "log", "marketplace": marketplace, "line": message,
            })

    # Для health/диагностики ------------------------------------------------

    def snapshot(self) -> dict:
        return {
            mp: {
                "running": self.running(mp),
                "desired": self._desired.get(mp) is not None,
            }
            for mp in MARKETPLACES
        }
