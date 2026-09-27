"""Файловый протокол воркера: pid, стейт, стоп-файл, свой лог-файл.

Почему файлы, а не сокеты/очереди: дашборд и воркеры — разные процессы, и
протокол на файлах уже доказал себя в ночном сборе (tools/night_harvest.py) —
он переживает падение любого из участников и читается любым инструментом
(дашборд, tail, jq). Стейт пишется атомарно (tmp + rename), чтобы дашборд
никогда не прочитал половину JSON.
"""

import json
import logging
import logging.handlers
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from ..config.settings import Settings

logger = logging.getLogger(__name__)

MARKETPLACES = ("ozon", "wb")

# Для тестов: позволяет уложить pid/стейт/стоп и базы во временную папку,
# не трогая живые data/.
_DATA_DIR_OVERRIDE = os.environ.get("WORKER_DATA_DIR", "")


def data_dir() -> Path:
    return Path(_DATA_DIR_OVERRIDE) if _DATA_DIR_OVERRIDE else Settings.BASE_DIR / "data"


def _worker_dir(marketplace: str, suffix: str) -> Path:
    return data_dir() / f"worker_{marketplace}{suffix}"


def pid_path(marketplace: str) -> Path:
    return _worker_dir(marketplace, ".pid")


def state_path(marketplace: str) -> Path:
    return _worker_dir(marketplace, ".state.json")


def stop_path(marketplace: str) -> Path:
    return _worker_dir(marketplace, ".stop")


def read_state(marketplace: str) -> Optional[dict]:
    """Текущий стейт воркера (для дашборда). None — воркер никогда не запускался."""
    try:
        return json.loads(state_path(marketplace).read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None


def pid_alive(marketplace: str) -> bool:
    """Жив ли процесс воркера по pid-файлу (как у night_harvest: os.kill(pid, 0))."""
    try:
        pid = int(pid_path(marketplace).read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def request_stop(marketplace: str) -> bool:
    """Просит воркера остановиться. Возвращает True, если был запущен."""
    if not pid_alive(marketplace):
        return False
    stop_path(marketplace).write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )
    return True


class WorkerRuntime:
    """Сторона воркера: пишет стейт, следит за стоп-файлом, ведёт лог."""

    STOP_POLL_SECONDS = 1.5

    def __init__(self, marketplace: str):
        if marketplace not in MARKETPLACES:
            raise ValueError(f"Неизвестный маркетплейс: {marketplace}")
        self.marketplace = marketplace
        self.stop_event = threading.Event()
        self._state: Dict = {}
        self._watcher: Optional[threading.Thread] = None

    # --- Жизненный цикл ---------------------------------------------------

    def acquire(self) -> None:
        """Захватывает воркер-слот: pid-файл, чистый стоп, стартовый стейт.

        Если pid-файл указывает на живой процесс — значит воркер уже работает
        (или остался после жёсткого kill вместе с системой): refuse, иначе
        появился бы второй пишущий в ту же базу.
        """
        if pid_alive(self.marketplace):
            raise RuntimeError(
                f"Воркер {self.marketplace} уже работает "
                f"(pid {pid_path(self.marketplace).read_text().strip()})"
            )
        data_dir().mkdir(parents=True, exist_ok=True)
        Settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # Протухший стоп-файл прошлого прогона надо убрать до старта, иначе
        # ватчер остановит воркера через полторы секунды.
        stop_path(self.marketplace).unlink(missing_ok=True)
        pid_path(self.marketplace).write_text(str(os.getpid()), encoding="utf-8")
        self._state = {
            "marketplace": self.marketplace,
            "pid": os.getpid(),
            "phase": "Запуск",
            "done": 0,
            "total": 0,
            "pages": 0,
            "products": 0,
            "sellers_found": 0,
            "sellers_added": 0,
            "sellers_rejected": 0,
            "errors": 0,
            "last_log": "",
            "blocked": False,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": "",
            "finished_at": None,
            "exit_status": None,
        }
        self.update()
        self._watcher = threading.Thread(
            target=self._watch_stop, name=f"stop-watcher-{self.marketplace}", daemon=True
        )
        self._watcher.start()

    def finish(self, status: str, message: str = "") -> None:
        """Финальный стейт + уборка pid-файла. status: ok | stopped | error."""
        self._state["exit_status"] = status
        self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if message:
            self._state["last_log"] = message
        self.update()
        pid_path(self.marketplace).unlink(missing_ok=True)
        self.stop_event.set()  # останавливаем ватчер

    # --- Стейт ------------------------------------------------------------

    def update(self, **fields) -> None:
        """Мерджит поля в стейт и атомарно пишет на диск."""
        self._state.update(fields)
        self._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path = state_path(self.marketplace)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    def update_from_stats(self, stats) -> None:
        """Переносит HarvestStats в стейт (поля совпадают по смыслу)."""
        self.update(
            pages=stats.pages,
            products=stats.products,
            sellers_found=stats.sellers_found,
            sellers_rejected=stats.sellers_rejected,
            errors=len(stats.errors),
            blocked=getattr(self, "_blocked", False),
        )

    def log(self, message: str) -> None:
        """Строка в лог + в стейт (для «живого» хвоста в дашборде)."""
        logger.info(message)
        self.update(last_log=message)

    # --- Остановка --------------------------------------------------------

    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def mark_blocked(self, reason: str) -> None:
        """Маркетплейс закрыл доступ (403/челлендж) — честный признак в UI."""
        self._blocked = True
        self.update(blocked=True, last_log=reason)

    def _watch_stop(self) -> None:
        """Следит за стоп-файлом и выставляет stop_event.

        Харвестеры проверяют threading.Event в своих циклах; файловый флаг —
        единственный канал «снаружи», доступный дашборду.
        """
        while not self.stop_event.is_set():
            if stop_path(self.marketplace).exists():
                logger.info("Получен стоп-файл — останавливаюсь")
                self.update(phase="Останавливаюсь")
                self.stop_event.set()
                return
            time.sleep(self.STOP_POLL_SECONDS)

    # --- Логи -------------------------------------------------------------

    def attach_file_logging(self) -> Path:
        """Добавляет к корневому логеру файл logs/worker_<mp>_ГГГГ-ММ-ДД.log."""
        log_file = Settings.LOGS_DIR / (
            f"worker_{self.marketplace}_{datetime.now():%Y-%m-%d}.log"
        )
        # Ротация: лог воркера пишется всю ночь без присмотра (аудит)
        handler = logging.handlers.RotatingFileHandler(
            log_file, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=2)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        return log_file
