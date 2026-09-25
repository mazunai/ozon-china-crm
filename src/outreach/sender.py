"""
Отправка сообщений продавцам через чат Ozon.

Как это работает
----------------
Отправщик подключается к уже запущенному Chrome с постоянным профилем
(`~/.ozon_parser_chrome`) — в нём вы один раз логинитесь в Ozon, и дальше
сессия переиспользуется. Никаких паролей в коде и никакого обхода защиты:
это ваш собственный браузер с вашей собственной сессией.

Что модуль делает сам
---------------------
Ограничители: интервал между сообщениями, дневной лимит, рабочее окно по
часам, стоп-кран файлом, режим предпросмотра без отправки. Логика ограничителей
вынесена в чистые функции (`should_send_now`, `plan_next_delay`) — они
покрыты тестами и не зависят от браузера.

Что нужно настроить один раз
----------------------------
Селекторы чата. Разметку чата Ozon нельзя угадать, поэтому её надо снять
один раз командой `python run_outreach.py inspect` из залогиненной сессии:
утилита сохранит найденные селекторы в `outreach_data/selectors.json`.
Пока файла нет, отправка откажется запускаться — это лучше, чем молча
отправлять не туда.
"""

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional, Tuple

from .queue_db import OutreachQueue

logger = logging.getLogger(__name__)


@dataclass
class SenderConfig:
    """Настройки отправки. Значения по умолчанию намеренно осторожные."""

    interval_minutes: int = 60          # пауза между сообщениями
    interval_jitter_minutes: int = 10   # случайный разброс паузы, чтобы не бить ровно в час
    daily_cap: int = 12                 # потолок отправок за сутки
    window_start: str = "10:00"         # рабочее окно: раньше не пишем
    window_end: str = "19:00"           # позже не пишем
    max_attempts: int = 2               # сколько раз пробовать одного продавца
    dry_run: bool = True                # по умолчанию НЕ отправляем, а показываем
    stop_file: str = "STOP"

    @classmethod
    def load(cls, path: Path) -> "SenderConfig":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            logger.warning(f"В {path.name} неизвестные ключи: {', '.join(sorted(unknown))}")
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )


@dataclass
class ChatSelectors:
    """Селекторы чата. Заполняются один раз командой `inspect`."""

    login_marker: str = ""     # элемент, видимый только в залогиненной сессии
    chat_button: str = ""      # кнопка «Чат» в блоке магазина на карточке товара
    message_input: str = ""    # поле ввода сообщения
    send_button: str = ""      # кнопка отправки
    sent_marker: str = ""      # необязательно: признак, что сообщение ушло

    @classmethod
    def load(cls, path: Path) -> "ChatSelectors":
        if not path.exists():
            raise FileNotFoundError(
                f"Селекторы чата не найдены: {path}\n"
                "Сначала выполните: python run_outreach.py inspect\n"
                "Команда откроет браузер с вашей сессией и сохранит селекторы."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def missing(self) -> list:
        required = ("login_marker", "chat_button", "message_input", "send_button")
        return [name for name in required if not getattr(self, name)]


# --- Ограничители: чистые функции, покрыты тестами -------------------------


def parse_window(start: str, end: str) -> Tuple[dtime, dtime]:
    def _parse(value: str) -> dtime:
        hours, minutes = value.split(":")
        return dtime(int(hours), int(minutes))

    return _parse(start), _parse(end)


def in_window(now: dtime, start: dtime, end: dtime) -> bool:
    """Внутри ли рабочего окна. Поддерживает окно через полночь (22:00–06:00)."""
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


@dataclass
class SendDecision:
    allowed: bool
    reason: str = ""
    retry_in_seconds: int = 0


def should_send_now(
    now: datetime,
    config: SenderConfig,
    sent_last_24h: int,
    next_allowed_at: Optional[datetime] = None,
) -> SendDecision:
    """Разрешать ли отправку прямо сейчас.

    Проверки по порядку: стоп-кран, дневной лимит, рабочее окно, интервал с
    прошлой отправки. Возвращает причину отказа и сколько ждать — это попадает
    в лог, чтобы в режиме ожидания было понятно, чего именно ждём.
    """
    if not config.dry_run and sent_last_24h >= config.daily_cap:
        return SendDecision(
            False,
            f"дневной лимит {config.daily_cap} исчерпан",
            retry_in_seconds=3600,
        )

    start, end = parse_window(config.window_start, config.window_end)
    if not in_window(now.time(), start, end):
        return SendDecision(
            False,
            f"вне рабочего окна {config.window_start}–{config.window_end}",
            retry_in_seconds=600,
        )

    if next_allowed_at and now < next_allowed_at:
        wait = int((next_allowed_at - now).total_seconds())
        return SendDecision(False, "пауза между сообщениями", retry_in_seconds=max(wait, 1))

    return SendDecision(True)


def plan_next_delay(config: SenderConfig, rng: Optional[random.Random] = None) -> int:
    """Сколько секунд ждать до следующего сообщения.

    Разброс нужен не для маскировки, а чтобы серия отправок не превращалась в
    идеально ровный метроном: живые люди пишут с разной скоростью, и ровный
    интервал — лишний повод для вопросов со стороны службы безопасности.
    """
    rng = rng or random.Random()
    base = max(config.interval_minutes, 0) * 60
    jitter = max(config.interval_jitter_minutes, 0) * 60
    return max(int(base + rng.uniform(-jitter, jitter)), 60)


def kill_switch_active(stop_file: Path) -> bool:
    """Файл-стоп-кран: создайте его, чтобы рассылка остановилась после текущего шага."""
    return stop_file.exists()


# --- Работа с браузером ---------------------------------------------------


class ChatClient:
    """Тонкая обёртка над DOM чата. Всё, что зависит от разметки Ozon, — здесь."""

    def __init__(self, driver, selectors: ChatSelectors, wait_seconds: int = 15):
        self.driver = driver
        self.selectors = selectors
        self.wait_seconds = wait_seconds

    def _find(self, selector: str):
        from selenium.webdriver.common.by import By

        by, value = selector.split("|", 1) if "|" in selector else ("css", selector)
        mapping = {
            "css": By.CSS_SELECTOR,
            "xpath": By.XPATH,
            "text": By.XPATH,  # text=Условия -> //*[contains(text(), ...)]
        }
        if by == "text":
            value = f"//*[contains(normalize-space(text()), {json.dumps(value)})]"
            by = "xpath"
        return self.driver.find_elements(mapping[by], value)

    def wait_for(self, selector: str, timeout: Optional[int] = None):
        from selenium.common.exceptions import TimeoutException

        deadline = time.time() + (timeout or self.wait_seconds)
        while time.time() < deadline:
            found = self._find(selector)
            if found:
                return found[0]
            time.sleep(0.5)
        raise TimeoutException(f"Не дождались элемента: {selector}")

    def is_logged_in(self) -> bool:
        return bool(self._find(self.selectors.login_marker))

    def open_chat(self, product_url: str) -> None:
        """Открывает карточку товара и раскрывает чат с продавцом."""
        from selenium.webdriver.common.by import By

        self.driver.get(product_url)
        button = self.wait_for(self.selectors.chat_button)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        time.sleep(0.5)
        try:
            button.click()
        except Exception:
            # Кнопку может перекрывать другой элемент — жмём через JS
            self.driver.execute_script("arguments[0].click();", button)

    def send(self, text: str) -> None:
        field = self.wait_for(self.selectors.message_input)
        field.click()
        field.clear()
        field.send_keys(text)
        time.sleep(0.3)
        self.wait_for(self.selectors.send_button).click()

    def confirm_sent(self) -> bool:
        """Если разметка позволяет подтвердить отправку — проверяем. Иначе считаем,
        что клик по кнопке отправки и есть отправка (Ozon не отдаёт квитанцию)."""
        if not self.selectors.sent_marker:
            return True
        return bool(self._find(self.selectors.sent_marker))


def build_driver(headless: bool = False):
    """Драйвер с постоянным профилем — в нём живёт ваша сессия Ozon."""
    from ..utils.selenium_manager import SeleniumManager

    manager = SeleniumManager(headless=headless)
    return manager, manager.create_driver()


# --- Основной цикл --------------------------------------------------------


def run_queue(
    queue: OutreachQueue,
    template,
    config: SenderConfig,
    data_dir: Path,
    once: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    now_fn: Callable[[], datetime] = datetime.now,
) -> dict:
    """Прогоняет очередь с соблюдением ограничителей.

    Возвращает сводку: сколько отправлено, сколько неудач, почему остановились.
    Если `once=True` — делает не больше одной отправки и выходит (удобно для
    ручной проверки, что всё настроено верно).
    """
    report = {"sent": 0, "failed": 0, "stopped_reason": "", "dry_run": config.dry_run}
    stop_file = data_dir / config.stop_file
    next_allowed_at: Optional[datetime] = None

    def progress(message: str) -> None:
        logger.info(message)
        if on_progress:
            on_progress(message)

    queue.release_stale()

    driver = None
    if not config.dry_run:
        _, driver = build_driver(headless=False)
        selectors = ChatSelectors.load(data_dir / "selectors.json")
        missing = selectors.missing()
        if missing:
            raise RuntimeError(f"Не заполнены селекторы: {', '.join(missing)}")
        client = ChatClient(driver, selectors)
        if not client.is_logged_in():
            raise RuntimeError(
                "В браузере нет залогиненной сессии Ozon. "
                "Войдите в аккаунт в открывшемся окне и запустите снова."
            )
    else:
        client = None

    try:
        while True:
            if kill_switch_active(stop_file):
                report["stopped_reason"] = f"сработал стоп-кран {stop_file.name}"
                progress(report["stopped_reason"])
                break

            decision = should_send_now(now_fn(), config, queue.sent_last_24h(), next_allowed_at)
            if not decision.allowed:
                if once:
                    report["stopped_reason"] = decision.reason
                    progress(f"Стоп: {decision.reason}")
                    break
                progress(f"Ждём {decision.retry_in_seconds} с — {decision.reason}")
                time.sleep(min(decision.retry_in_seconds, 300))
                continue

            row = queue.claim_next()
            if not row:
                report["stopped_reason"] = "очередь пуста"
                progress(report["stopped_reason"])
                break

            seller_id = row["seller_id"]
            shop = row["shop_name"] or seller_id
            message = template.render(dict(row))

            if config.dry_run:
                progress(f"[предпросмотр] {shop} — кому: {row['sample_product_url']}")
                progress(f"[предпросмотр] текст:\n{message}")
                queue.mark_skipped(seller_id)
                report["stopped_reason"] = "режим предпросмотра"
                break

            try:
                client.open_chat(row["sample_product_url"])
                client.send(message)
                if client.confirm_sent():
                    queue.mark_sent(seller_id, message)
                    report["sent"] += 1
                else:
                    queue.mark_failed(seller_id, "не подтвердилась отправка", retryable=True)
                    report["failed"] += 1
            except Exception as exc:                       # noqa: BLE001
                retryable = int(row["attempts"] or 0) < config.max_attempts
                queue.mark_failed(seller_id, str(exc), retryable=retryable)
                report["failed"] += 1

            next_allowed_at = now_fn() + timedelta(seconds=plan_next_delay(config))

            if once:
                report["stopped_reason"] = "режим одной отправки"
                break
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:                              # noqa: BLE001
                pass

    return report
