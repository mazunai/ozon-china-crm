"""Автономная обработка очереди: супервизор, который гоняет раунды отправки
без участия оператора и требует человека только в неоднозначной ситуации.

Как это устроено
----------------
Супервизор НЕ дублирует логику отправки. Раунд — это обычный запуск
существующего движка (`dashboard.server.SEND.start()` со всеми его проверками:
ночной сбор, воркеры, замок браузера, preflight). Супервизор только решает,
когда запустить следующий раунд, копит статистику и останавливается там, где
нужен человек:

* очередь пуста — спит подольше и проверяет снова (сбор мог добавить новых);
* серия сбоев — Sender сам ставит backoff и предохранитель, раунд завершится
  с понятной причиной, супервизор сделает паузу и продолжит;
* Risk Control (ограничение платформы) — отправщик ставит стоп-кран и пишет
  инцидент в базу; супервизор входит в паузу «до оператора» и НЕ возобновляет
  отправку сам. Снять паузу может только человек: /resume в Telegram,
  POST /api/autonomous/resume или `python run_autonomous.py resume`.

Состояние живёт в `data/autonomous_state.json` (атомарная запись), живой
процесс — в `data/autonomous.lock` (pid). Дашборд при старте видит
«running=true» и мёртвый лок — и сам продолжает режим, как ночной сбор.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TICK_SECONDS = 5  # шаг служебного цикла: проверка файлов и флагов


class LockBusy(RuntimeError):
    """Автономный режим уже работает в другом процессе."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _atomic_write_json(path: Path, data: dict) -> None:
    """Атомарная запись стейта: temp + rename, читатели не видят полуфайл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class AutonomousRunner:
    """Логика автономного режима без привязки к дашборду.

    `start_round` — колбэк, запускающий один раунд отправки (в дашборде это
    SEND.start, в тестах — заглушка). `round_running` — идёт ли раунд сейчас.
    Всё остальное — паузы, счётчики, стоп-файлы, риск-пауза — здесь.
    """

    def __init__(self, *, data_dir: Path, state_path: Path, lock_path: Path,
                 stop_path: Path, tg_send: Optional[Callable[[str], bool]] = None):
        self.data_dir = Path(data_dir)
        self.state_path = Path(state_path)
        self.lock_path = Path(lock_path)
        self.stop_path = Path(stop_path)
        self.tg_send = tg_send or (lambda text: False)
        self._lock = threading.Lock()
        self.state: dict = self._load_state()

    # --- состояние -------------------------------------------------------- #

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_state(self) -> None:
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(self.state_path, self.state)

    def status(self) -> dict:
        with self._lock:
            return dict(self.state)

    # --- файлы-краны -------------------------------------------------------- #

    def graceful_stop_requested(self) -> bool:
        return self.stop_path.exists()

    def request_graceful_stop(self) -> None:
        """Штатная остановка: дописать текущего продавца и выйти из режима."""
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(
            f"graceful stop {datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
        logger.info("Автономный режим: запрошена штатная остановка")

    def cancel_graceful_stop(self) -> None:
        if self.stop_path.exists():
            self.stop_path.unlink()

    def risk_stop_active(self) -> bool:
        """Стоп-кран отправщика на месте — обычно его ставит Risk Control."""
        return (self.data_dir / "STOP").exists()

    # --- лок единственности ------------------------------------------------- #

    def take_lock(self) -> None:
        """Захватывает лок или бросает LockBusy — режим живёт в одном экземпляре."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise LockBusy(f"Автономный режим уже работает (pid {pid})")
            # Мёртвый pid — остаток аварийного завершения, лок переходим
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_lock(self) -> None:
        try:
            if self.lock_path.exists() and \
                    self.lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.lock_path.unlink()
        except OSError:
            pass

    # --- возобновление после ограничения ------------------------------------ #

    def resume(self, store) -> dict:
        """Действие оператора после разбора ограничения.

        Закрывает открытый инцидент, снимает стоп-кран — и следующий раунд
        уйдёт сам. Если инцидента не было, просто снимает кран (оператор мог
        поставить его руками через /api/send/stopfile).
        """
        resolved = False
        incident = store.open_risk_incident() if hasattr(store, "open_risk_incident") else None
        if incident is not None and hasattr(store, "resolve_risk_incident"):
            resolved = bool(store.resolve_risk_incident("operator"))
        stop_file = self.data_dir / "STOP"
        if stop_file.exists():
            stop_file.unlink()
        # Аккаунтные стоп-краны (STOP_<id> от Risk Control): оператор
        # разобрался — снимаем всем. Срок платформы обычно один на машину
        # (IP/устройство общие), держать аккаунты заблокированными дольше
        # названного срока нечего.
        for extra in self.data_dir.glob("STOP_*"):
            try:
                extra.unlink()
            except OSError:
                logger.warning(f"Не снял аккаунтный стоп-кран {extra.name}")
        with self._lock:
            self.state["paused_reason"] = ""
            self._save_state()
        self.tg_send("▶️ Автономная обработка возобновлена оператором"
                     + (f" (инцидент #{incident['id']} закрыт)" if incident else ""))
        logger.info(f"Возобновлено оператором, инцидент закрыт: {resolved}")
        return {"resolved_incident": resolved, "had_incident": bool(incident)}

    # --- основной цикл ------------------------------------------------------- #

    def run_forever(self, *, start_round: Callable[[], None],
                    round_running: Callable[[], bool],
                    round_snapshot: Optional[Callable[[], dict]] = None,
                    sleep_fn: Callable[[float], None] = time.sleep) -> None:
        """Крутит раунды до штатной остановки.

        Все колбэки внешние: супервизор не знает ни про браузер, ни про
        дашборд — только «запусти раунд», «идёт ли», «что получилось».
        `round_snapshot` отдаёт итог раунда (sent/failed/stopped_reason) —
        в дашборде это SEND.state после его завершения.
        """
        self.take_lock()
        round_snapshot = round_snapshot or (lambda: {})
        try:
            with self._lock:
                self.state["running"] = True
                self.state.setdefault("rounds", 0)
                self.state.setdefault("sent", 0)
                self.state.setdefault("failed", 0)
                self.state.setdefault("skipped", 0)
                self.state.setdefault("started_at",
                                      datetime.now().isoformat(timespec="seconds"))
                self.state["paused_reason"] = ""
                self._save_state()
            self._notify("🤖 Автономная обработка запущена")

            paused_reason = ""
            while not self.graceful_stop_requested():
                if round_running():
                    sleep_fn(TICK_SECONDS)
                    continue

                # Риск-пауза: стоп-кран стоит, инцидент открыт. Ждём оператора
                # сколько нужно — сам не снимаем никогда.
                if self.risk_stop_active():
                    if paused_reason != "risk":
                        paused_reason = "risk"
                        with self._lock:
                            self.state["paused_reason"] = (
                                "ограничение платформы — жду оператора (/resume)")
                            self._save_state()
                        self._notify(
                            "⚠️ Автономная обработка остановлена: ограничение "
                            "платформы. Проверьте причину и выполните /resume. "
                            "Автопродолжения нет.")
                    sleep_fn(TICK_SECONDS)
                    continue

                if paused_reason == "risk":
                    # Кран снят мимо resume (руками из дашборда) — инцидент
                    # всё равно закрываем, чтобы журнал не врал.
                    paused_reason = ""
                    with self._lock:
                        self.state["paused_reason"] = ""
                        self._save_state()

                with self._lock:
                    self.state["rounds"] = int(self.state.get("rounds", 0)) + 1
                    self._save_state()
                logger.info(f"Автономный раунд #{self.state['rounds']}: старт")
                try:
                    start_round()
                except Exception as exc:                     # noqa: BLE001
                    # Раунд не смог даже начаться (занят браузер, нет сессии).
                    # Это не риск-сигнал — обычная пауза и следующая попытка.
                    logger.warning(f"Раунд не запустился: {exc}")
                    with self._lock:
                        self.state["last_error"] = str(exc)[:400]
                        self._save_state()
                    self._wait(minutes=10, sleep_fn=sleep_fn,
                               extra_check=self.graceful_stop_requested)
                    continue

                # Ждём завершения раунда
                while round_running() and not self.graceful_stop_requested():
                    sleep_fn(TICK_SECONDS)

                snap = round_snapshot() or {}
                with self._lock:
                    self.state["sent"] = int(self.state.get("sent", 0)) + int(snap.get("sent", 0) or 0)
                    self.state["failed"] = int(self.state.get("failed", 0)) + int(snap.get("failed", 0) or 0)
                    self.state["skipped"] = int(self.state.get("skipped", 0)) + int(snap.get("skipped", 0) or 0)
                    self.state["last_finish_reason"] = str(snap.get("stopped_reason", "") or "")
                    self.state["last_error"] = str(snap.get("error", "") or "")
                    self._save_state()
                logger.info(
                    f"Раунд завершён: отправлено {snap.get('sent', 0)}, "
                    f"ошибок {snap.get('failed', 0)}"
                    + (f", причина: {snap.get('stopped_reason')}" if snap.get("stopped_reason") else ""))

                reason = str(snap.get("stopped_reason", "") or "")
                if "ограничение платформы" in reason:
                    # Инцидент уже записан отправщиком; дальше — только оператор.
                    paused_reason = "risk"
                    continue
                if "остановлено вручную" in reason or "стоп-кран" in reason:
                    # Оператор остановил раунд руками — крутить дальше поверх
                    # его воли нельзя.
                    self._notify("🛑 Автономный режим остановлен: раунд остановлен вручную")
                    break
                if "очередь пуста" in reason:
                    self._wait(minutes=60, sleep_fn=sleep_fn,
                               extra_check=self.graceful_stop_requested)
                else:
                    self._wait(minutes=20, sleep_fn=sleep_fn,
                               extra_check=self.graceful_stop_requested)

            with self._lock:
                self.state["running"] = False
                # Если остановились в риск-паузе — честно оставляем её причину:
                # «штатно» здесь было бы враньём в журнале и в Telegram.
                self.state["paused_reason"] = (
                    "ограничение платформы — жду оператора (/resume)"
                    if paused_reason == "risk" else "штатная остановка")
                self._save_state()
            self._notify(
                "⚠️ Автономная обработка остановлена в риск-паузе — нужно /resume"
                if paused_reason == "risk"
                else "🤖 Автономная обработка остановлена (штатно)")
        finally:
            self.release_lock()

    def _wait(self, minutes: int, sleep_fn, extra_check: Callable[[], bool]) -> None:
        """Пауза с проверкой кранов каждые TICK_SECONDS — остановка не ждёт конца паузы."""
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            if extra_check():
                return
            sleep_fn(TICK_SECONDS)

    def _notify(self, text: str) -> None:
        try:
            self.tg_send(text)
        except Exception:                                    # noqa: BLE001
            logger.warning("Не отправил уведомление в Telegram", exc_info=True)


class Supervisor:
    """Обёртка над AutonomousRunner для дашборда: поток, старт/стоп/статус."""

    def __init__(self, runner: AutonomousRunner, *, start_round, round_running,
                 round_snapshot=None):
        self.runner = runner
        self._start_round = start_round
        self._round_running = round_running
        self._round_snapshot = round_snapshot
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("Автономный режим уже работает")
            self._thread = threading.Thread(
                target=self.runner.run_forever,
                kwargs={"start_round": self._start_round,
                        "round_running": self._round_running,
                        "round_snapshot": self._round_snapshot},
                daemon=True, name="autonomous",
            )
            self._thread.start()

    def stop(self) -> None:
        self.runner.request_graceful_stop()

    def resume(self, store) -> dict:
        return self.runner.resume(store)

    def status(self) -> dict:
        st = self.runner.status()
        st["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return st

    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
