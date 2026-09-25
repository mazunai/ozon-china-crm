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
from ..crm.store import STATUS_NEW

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

    # Рабочее окно ограничивает только реальную отправку. Предпросмотр ничего
    # не отправляет и должен показывать текст в любое время суток — иначе
    # вечером он встаёт в бесконечное ожидание до утра.
    if not config.dry_run:
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

    def __init__(self, driver, selectors: ChatSelectors, wait_seconds: int = 15, manager=None):
        self.driver = driver
        self.selectors = selectors
        self.wait_seconds = wait_seconds
        self.manager = manager

    def _focus(self) -> None:
        focus_tab(self.manager)

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
        self._focus()
        return bool(self._find(self.selectors.login_marker))

    def open_chat(self, seller_id: str) -> None:
        """Открывает чат с продавцом через его страницу.

        Схема снята с живой страницы Ozon: на странице магазина ровно одна
        кнопка «Написать», она ведёт в чат с этим продавцом. Прямая ссылка на
        чат требует подписанного токена `url_sign`, который генерируется на
        клиенте при нажатии, поэтому воспроизводим именно нажатие.

        Раньше открывалась карточка товара и искалась кнопка «Чат» — теперь
        товар не нужен, достаточно seller_id.
        """
        self._focus()
        self.driver.get(f"https://www.ozon.ru/seller/{seller_id}/")
        self._focus()
        button = self.wait_for(self.selectors.chat_button)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        time.sleep(0.5)
        try:
            button.click()
        except Exception:
            # Кнопку может перекрывать другой элемент — жмём через JS
            self.driver.execute_script("arguments[0].click();", button)
        # Ждём поле ввода: значит чат действительно открылся
        self.wait_for(self.selectors.message_input, timeout=max(self.wait_seconds, 25))

    def send(self, text: str) -> None:
        """Печатает текст и нажимает отправку.

        Кнопка отправки заблокирована, пока поле пустое, и разблокируется после
        ввода — проверено на живой странице.
        """
        self._focus()
        field = self.wait_for(self.selectors.message_input)
        field.click()
        field.clear()
        field.send_keys(text)
        time.sleep(0.5)
        button = self.wait_for(self.selectors.send_button)
        try:
            button.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", button)

    def field_is_empty(self) -> bool:
        """Опустело ли поле. Косвенный признак того, что сообщение ушло."""
        try:
            field = self.wait_for(self.selectors.message_input, timeout=5)
            value = field.get_attribute("value") or ""
            if not value:
                value = self.driver.execute_script(
                    "return arguments[0].value || arguments[0].innerText || ''", field) or ""
            return not str(value).strip()
        except Exception:                                    # noqa: BLE001
            return False

    def confirm_sent(self) -> bool:
        """Подтверждает отправку разметкой, а если её нет — очисткой поля.

        Ozon не отдаёт квитанцию, но поле ввода очищается после успешной
        отправки. Это честнее, чем «нажали кнопку — значит отправили»: при
        сбое текст останется в поле, и мы отметим попытку как неудачную.
        """
        self._focus()
        if self.selectors.sent_marker:
            return bool(self._find(self.selectors.sent_marker))
        time.sleep(1.0)
        return self.field_is_empty()


def preflight(config: SenderConfig, template, data_dir: Path, store=None,
              now: Optional[datetime] = None) -> dict:
    """Проверяет всё, что нужно для отправки, и возвращает готовность.

    Раньше эти проверки были разбросаны: часть в CLI, часть в тексте README,
    часть вообще нигде. Панель, вызывающая отправку напрямую, могла разослать
    шаблон-заготовку «ЗАМЕНИТЕ ЭТОТ ТЕКСТ», потому что проверку делал только
    разбор аргументов командной строки.
    """
    now = now or datetime.now()
    checks = []

    def add(key: str, ok: bool, title: str, detail: str = "", blocking: bool = True) -> None:
        checks.append({"key": key, "ok": bool(ok), "title": title,
                       "detail": detail, "blocking": blocking})

    # Шаблон
    if template is None:
        add("template", False, "Текст сообщения", "шаблон не загружен")
    elif getattr(template, "is_placeholder", lambda: False)():
        add("template", False, "Текст сообщения",
            "в шаблоне осталась метка-заготовка — напишите свой текст")
    else:
        add("template", True, "Текст сообщения", f"«{template.name}»",
            blocking=False)

    # Селекторы чата
    selectors_path = data_dir / "selectors.json"
    if not selectors_path.exists():
        add("selectors", False, "Разметка чата",
            "нет selectors.json — выполните: python run_outreach.py inspect")
    else:
        try:
            selectors = ChatSelectors.load(selectors_path)
            missing = selectors.missing()
            if missing:
                add("selectors", False, "Разметка чата",
                    "не заполнено: " + ", ".join(missing))
            else:
                add("selectors", True, "Разметка чата", "настроена", blocking=False)
        except Exception as exc:                           # noqa: BLE001
            add("selectors", False, "Разметка чата", str(exc)[:120])

    # Стоп-кран
    stop_file = data_dir / config.stop_file
    if kill_switch_active(stop_file):
        add("stop", False, "Стоп-кран", f"файл {stop_file.name} на месте — отправка остановлена")
    else:
        add("stop", True, "Стоп-кран", "нет", blocking=False)

    # Дневной лимит
    sent_today = store.sent_today() if store is not None and hasattr(store, "sent_today") else 0
    left = max(0, config.daily_cap - sent_today)
    add("cap", left > 0, "Дневной лимит",
        f"отправлено сегодня {sent_today} из {config.daily_cap}"
        + ("" if left else " — предел достигнут"),
        blocking=not config.dry_run and left == 0)

    # Рабочее окно
    start, end = parse_window(config.window_start, config.window_end)
    inside = in_window(now.time(), start, end)
    add("window", inside, "Рабочее окно",
        f"{config.window_start}–{config.window_end}, сейчас "
        + ("внутри" if inside else "вне окна"),
        blocking=not config.dry_run and not inside)

    # Очередь
    if store is not None and hasattr(store, "queue_health"):
        health = store.queue_health()
        add("queue", health["ready"] > 0, "Очередь",
            f"готово к отправке {health['ready']}"
            + (f", с ошибкой {health['failed']}" if health["failed"] else ""),
            blocking=False)
    else:
        add("queue", True, "Очередь", "не проверялась", blocking=False)

    blocking_failed = [c for c in checks if c["blocking"] and not c["ok"]]
    return {
        "checks": checks,
        "ready": not blocking_failed,
        "blocking": [c["key"] for c in blocking_failed],
        "dry_run": config.dry_run,
        "sent_today": sent_today,
        "daily_cap": config.daily_cap,
    }


def login_flow(data_dir: Path, timeout_seconds: int = 300, on_progress=None) -> bool:
    """Открывает Chrome с профилем парсера, чтобы вы вошли в Ozon.

    Профиль парсера (`~/.ozon_parser_chrome`) отдельный от вашего обычного
    браузера: сессия из него не переносится. Поэтому вход нужно сделать один
    раз именно в этом окне — дальше cookies сохранятся и отправка будет
    работать без входа.

    Функция ждёт, пока вход действительно произойдёт, и только потом отдаёт
    управление: иначе отправка упала бы на первом же продавце.
    """
    from ..utils.selenium_manager import SeleniumManager

    def say(message: str) -> None:
        logger.info(message)
        if on_progress:
            on_progress(message)

    manager = SeleniumManager(headless=False)
    driver = manager.create_driver()
    try:
        driver.get("https://www.ozon.ru/")
        say("Открыл Ozon в профиле парсера. Войдите в аккаунт в этом окне.")

        selectors_path = data_dir / "selectors.json"
        selectors = ChatSelectors.load(selectors_path)
        client = ChatClient(driver, selectors, manager=manager)

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if client.is_logged_in():
                say("Вход выполнен — сессия сохранена в профиле парсера.")
                return True
            time.sleep(3)

        say(f"Вход не выполнен за {timeout_seconds} секунд.")
        return False
    finally:
        try:
            manager.close()
        except Exception:                                    # noqa: BLE001
            pass


def build_driver(headless: bool = False):
    """Драйвер с постоянным профилем — в нём живёт ваша сессия Ozon.

    Возвращаем и менеджер: через него закрываем только свою вкладку (а не весь
    Chrome) и переключаемся на неё перед каждой операцией.
    """
    from ..utils.selenium_manager import SeleniumManager

    manager = SeleniumManager(headless=headless)
    return manager, manager.create_driver()


def focus_tab(manager) -> None:
    """Делает активной вкладку этого процесса.

    Без этого при параллельном сборе или парсинге активной может оказаться
    чужая вкладка, и ввод текста уйдёт не на ту страницу — тот же класс бага,
    что был у парсеров с перепутанными данными продавцов.
    """
    if manager is None:
        return
    try:
        manager._focus_tab()
    except Exception:                                      # noqa: BLE001
        pass


# --- Основной цикл --------------------------------------------------------


def run_queue(
    queue: OutreachQueue,
    template,
    config: SenderConfig,
    data_dir: Path,
    once: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    now_fn: Callable[[], datetime] = datetime.now,
    stop_event=None,
    on_event: Optional[Callable[[dict], None]] = None,
    limit: Optional[int] = None,
) -> dict:
    """Прогоняет очередь с соблюдением ограничителей.

    Возвращает сводку: сколько отправлено, сколько неудач, почему остановились.
    Если `once=True` — делает не больше одной отправки и выходит (удобно для
    ручной проверки, что всё настроено верно).
    """
    report = {"sent": 0, "failed": 0, "stopped_reason": "", "dry_run": config.dry_run}
    stop_file = data_dir / config.stop_file
    next_allowed_at: Optional[datetime] = None

    def cancelled() -> bool:
        """Отмена: файл-стоп-кран или внешний флаг."""
        if kill_switch_active(stop_file):
            return True
        return bool(stop_event is not None and stop_event.is_set())

    def progress(message: str) -> None:
        logger.info(message)
        if on_progress:
            on_progress(message)

    def emit(kind: str, **payload) -> None:
        """Структурное событие для интерфейса: без вылавливания чисел из текста."""
        if on_event:
            try:
                on_event({"kind": kind, **payload})
            except Exception:                              # noqa: BLE001
                pass

    def rest(seconds: int) -> bool:
        """Пауза с проверкой отмены. Возвращает False, если пора выходить.

        Раньше тут был один time.sleep до 300 секунд без проверок: нажатие
        «Остановить» могло ждать до пяти минут.
        """
        deadline = time.time() + max(0, seconds)
        while time.time() < deadline:
            if cancelled():
                return False
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
        return not cancelled()

    queue.release_stale()

    driver = None
    manager = None
    if not config.dry_run:
        manager, driver = build_driver(headless=False)
        selectors = ChatSelectors.load(data_dir / "selectors.json")
        missing = selectors.missing()
        if missing:
            raise RuntimeError(f"Не заполнены селекторы: {', '.join(missing)}")
        client = ChatClient(driver, selectors, manager=manager)
        if not client.is_logged_in():
            raise RuntimeError(
                "В браузере нет залогиненной сессии Ozon. "
                "Войдите в аккаунт в открывшемся окне и запустите снова."
            )
    else:
        client = None

    try:
        while True:
            if cancelled():
                report["stopped_reason"] = (
                    "сработал стоп-кран" if kill_switch_active(stop_file) else "остановлено вручную"
                )
                progress(report["stopped_reason"])
                break

            decision = should_send_now(now_fn(), config, queue.sent_today(), next_allowed_at)
            if not decision.allowed:
                if once:
                    report["stopped_reason"] = decision.reason
                    progress(f"Стоп: {decision.reason}")
                    break
                emit("waiting", reason=decision.reason, seconds=decision.retry_in_seconds)
                progress(f"Ждём {decision.retry_in_seconds} с — {decision.reason}")
                if not rest(min(decision.retry_in_seconds, 300)):
                    continue
                continue

            # Предпросмотр НЕ трогает очередь: раньше claim_next списывал попытку
            # и ставил статус «пропущен» до проверки dry_run, то есть один клик
            # «предпросмотр» сжигал продавца. Теперь просто показываем первого
            # кандидата, ничего не меняя.
            if config.dry_run:
                candidates = queue.list_sellers(
                    verified_only=True, statuses=[STATUS_NEW], limit=1, offset=0
                ) if hasattr(queue, "list_sellers") else []
                if not candidates:
                    report["stopped_reason"] = "очередь пуста"
                    progress(report["stopped_reason"])
                    break
                row = candidates[0]
                shop = row["shop_name"] or row["seller_id"]
                message = template.render(dict(row))
                progress(f"[предпросмотр] {shop} — кому: {row['sample_product_url']}")
                progress(f"[предпросмотр] текст:\n{message}")
                emit("preview", seller_id=row["seller_id"], shop=shop, message=message)
                report["stopped_reason"] = "режим предпросмотра"
                report["preview"] = {"seller_id": row["seller_id"], "shop": shop, "message": message}
                break

            row = queue.claim_next()
            if not row:
                report["stopped_reason"] = "очередь пуста"
                progress(report["stopped_reason"])
                break

            # Ограничение прогона: удобно для проверки на нескольких
            # продавцах, не дожидаясь дневного лимита
            if limit is not None and report["sent"] >= limit:
                report["stopped_reason"] = f"достигнут предел прогона: {limit}"
                progress(report["stopped_reason"])
                break

            seller_id = row["seller_id"]
            shop = row["shop_name"] or seller_id
            message = template.render(dict(row))
            emit("sending", seller_id=seller_id, shop=shop)

            try:
                client.open_chat(row["seller_id"])
                client.send(message)
                if client.confirm_sent():
                    queue.mark_sent(seller_id, message)
                    report["sent"] += 1
                    emit("sent", seller_id=seller_id, shop=shop, total=report["sent"])
                else:
                    queue.mark_failed(seller_id, "не подтвердилась отправка", retryable=True)
                    if hasattr(queue, "log_message"):
                        queue.log_message(seller_id, message, "failed", "не подтвердилась отправка")
                    report["failed"] += 1
                    emit("failed", seller_id=seller_id, shop=shop, error="не подтвердилась отправка")
            except Exception as exc:                       # noqa: BLE001
                retryable = int(row["attempts"] or 0) < config.max_attempts
                queue.mark_failed(seller_id, str(exc), retryable=retryable)
                if hasattr(queue, "log_message"):
                    queue.log_message(seller_id, message, "failed", str(exc))
                report["failed"] += 1
                emit("failed", seller_id=seller_id, shop=shop, error=str(exc)[:200])

            next_allowed_at = now_fn() + timedelta(seconds=plan_next_delay(config))

            if once:
                report["stopped_reason"] = "режим одной отправки"
                break
    finally:
        # Закрываем ТОЛЬКО свою вкладку: Chrome общий, в нём живёт сессия Ozon.
        # Раньше здесь был driver.quit(), который убивал браузер целиком —
        # в отличие от парсеров, которые оставляют его живым.
        if manager is not None:
            try:
                manager.close()
            except Exception:                              # noqa: BLE001
                pass

    return report
