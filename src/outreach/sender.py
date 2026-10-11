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
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .queue_db import OutreachQueue
from .accounts import SenderAccount
from .templates import TemplatePool
from ..crm.risk import PlatformRestricted, load_extra_markers
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
    fresh_first: bool = False           # сначала только что найденные сбором
    dry_run: bool = True                # по умолчанию НЕ отправляем, а показываем
    stop_file: str = "STOP"
    # Backoff при СЕРИЙНЫХ сбоях (сеть, браузер, страница не та). Это не обход
    # антифрода: пауза нужна, чтобы техническая авария не превращалась в
    # бесконечный долбёж по очереди. При признаках ограничения платформы
    # работает Risk Control (полная остановка), а не этот backoff.
    backoff_base_minutes: int = 5       # пауза после 1-го сбоя серии
    backoff_max_minutes: int = 60       # потолок паузы (удвоение до него)
    max_consecutive_failures: int = 5   # столько сбоев подряд — стоп прогона
    # Аккаунт отправителя (у менеджеров их несколько) и политика повторных
    # контактов. История у продавца ОДНА общая: перед отправкой смотрим в неё
    # целиком, а не только в историю текущего аккаунта.
    sender_account_id: str = "main"
    repeat_contact_policy: str = "never"  # never | after_days
    repeat_after_days: int = 30           # для after_days: минимум дней с контакта
    # Режим «по кругу»: пустая очередь — не финиш, а ожидание пополнения
    # (свежие продавцы приходят сбором). Прогон крутится сутки напролёт:
    # дневной лимит и рабочее окно по-прежнему действуют.
    loop: bool = False
    # Плавный разгон свежего аккаунта: дневной лимит растёт линейно от
    # ramp_min_cap до daily_cap за ramp_days суток. 0 — сразу полный лимит.
    # Дата старта фиксируется при первом запуске (ramp_start_date).
    ramp_days: int = 0
    ramp_min_cap: int = 4
    # Авто-замедление: при всплеске отказов или нулевой ответности за неделю
    # дневной лимит режется (adaptive_cap_factor) — сигнал «жмём», а не повод
    # гнать объём дальше.
    adaptive_slowdown: bool = True
    # Фильтрация целевого сегмента продавцов
    min_orders: Optional[int] = None
    max_orders: Optional[int] = None
    min_reviews: Optional[int] = None
    max_reviews: Optional[int] = None
    allowed_templates: List[str] = field(default_factory=list)

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
    """Внутри ли рабочего окна.

    Поддерживает окно через полночь (22:00–06:00). Границы, равные друг другу
    (00:00–00:00), означают круглосуточно: иначе окно вырождалось бы в одну
    секунду полуночи, и круглосуточный режим через настройки было не задать.
    """
    if start == end:
        return True
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
    effective_cap: Optional[int] = None,
) -> SendDecision:
    """Разрешать ли отправку прямо сейчас.

    Проверки по порядку: стоп-кран, дневной лимит, рабочее окно, интервал с
    прошлой отправки. Возвращает причину отказа и сколько ждать — это попадает
    в лог, чтобы в режиме ожидания было понятно, чего именно ждём.

    `effective_cap` — сегодняшний лимит с учётом разгона, суточного джиттера
    и авто-замедления (effective_daily_cap); без него берётся config.daily_cap.
    """
    cap = max(effective_cap, 1) if effective_cap is not None else config.daily_cap
    if not config.dry_run and sent_last_24h >= cap:
        return SendDecision(
            False,
            f"дневной лимит {cap} исчерпан",
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


def failed_round_pause_seconds(rng: Optional[random.Random] = None) -> int:
    """Пауза после витка, где сообщение НЕ ушло: чат закрыт, сбой виджета,
    ошибка отправки.

    Продавец ничего не получил, очередь не сдвинулась — долго стоять незачем.
    Пара секунд только чтобы не превращать проход по очереди из закрытых
    личек в пулемётную очередь страниц: каждая из них всё же визит в Ozon.
    """
    rng = rng or random.Random()
    return rng.randint(5, 15)


def ramp_daily_cap(target: int, ramp_days: int, start_cap: int, days_elapsed: int) -> int:
    """Дневной лимит на N-й день разгона: линейно от start_cap к target.

    Свежий аккаунт, который в первый же день пишет 20+ человек, выглядит
    как включённый скрипт: объём должен расти по мере «прогрева» истории.
    """
    if ramp_days <= 0 or days_elapsed >= ramp_days:
        return max(target, 1)
    start_cap = max(min(start_cap, target), 1)
    progress = max(days_elapsed, 0) / ramp_days
    return max(round(start_cap + progress * (target - start_cap)), 1)


def day_jitter_cap(cap: int, day_key: str, spread: float = 0.1) -> int:
    """Случайное отклонение дневного лимита, стабильное внутри суток.

    Ровно 24 каждый день — тоже паттерн: живой объём гуляет туда-сюда.
    Зерно — дата, поэтому в течение дня число не меняется и попадает в лог
    одинаково при каждом обновлении статуса.
    """
    rng = random.Random(f"daily-cap:{day_key}")
    return max(round(cap * (1 + rng.uniform(-spread, spread))), 1)


def adaptive_cap_factor(replied: int, negative: int, contacted: int,
                        min_sample: int = 20) -> float:
    """Множитель дневного лимита по откликам получателей за окно.

    Мало данных — не судим. Каждый пятый отказ или просьба не писать —
    «жмём», лимит вполовину. Ноль ответов при достаточной выборке — текст
    или таргетинг не работают: минус четверть, а не ускорение.
    """
    if contacted < min_sample:
        return 1.0
    if negative / contacted > 0.2:
        return 0.5
    if replied == 0:
        return 0.75
    return 1.0


def ramp_start_date(data_dir: Path, today: date) -> date:
    """Дата старта разгона — день первого запуска с включённым режимом.

    Живёт в outreach_data/ramp.json: перезапуск прогона разгон не сбрасывает,
    «начать заново» = удалить файл. Кривой или нечитаемый файл считается
    новым стартом: безопаснее медленный старт, чем внезапный полный лимит.
    """
    path = Path(data_dir) / "ramp.json"
    if path.exists():
        try:
            return date.fromisoformat(
                json.loads(path.read_text(encoding="utf-8"))["started"])
        except (OSError, ValueError, KeyError):
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"started": today.isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return today


def effective_daily_cap(config: "SenderConfig", now: datetime,
                        feedback: Optional[dict] = None,
                        ramp_started: Optional[date] = None) -> int:
    """Дневной лимит на сегодня: разгон × суточный джиттер × авто-замедление.

    `ramp_started` — дата старта разгона (ramp_start_date), `feedback` —
    словарь outreach_feedback() из базы. Оба параметра опциональны: нет
    данных — соответствующий множитель просто не применяется.
    """
    cap = config.daily_cap
    if config.ramp_days > 0 and ramp_started is not None:
        cap = ramp_daily_cap(cap, config.ramp_days, config.ramp_min_cap,
                             (now.date() - ramp_started).days)
    cap = day_jitter_cap(cap, now.date().isoformat())
    if config.adaptive_slowdown and feedback:
        cap = round(cap * adaptive_cap_factor(feedback.get("replied", 0),
                                              feedback.get("negative", 0),
                                              feedback.get("contacted", 0)))
    return max(cap, 1)


def backoff_seconds(consecutive_failures: int, base_minutes: int, max_minutes: int) -> int:
    """Пауза после N сбоев подряд: удвоение от base до потолка max.

    Чистая функция — покрыта тестами. Первый сбой серии ещё не серия: пауза
    начинается со второго подряд (consecutive_failures >= 2).
    """
    if consecutive_failures < 2:
        return 0
    base = max(base_minutes, 0) * 60
    ceiling = max(max_minutes, base_minutes) * 60
    return min(base * (2 ** (consecutive_failures - 2)), ceiling)


def repeat_contact_allowed(policy: str, after_days: int, prior_sent_at: str,
                           now: datetime) -> Tuple[bool, str]:
    """Можно ли писать продавцу, у которого уже есть успешная отправка.

    Глобальная защита от дублей между аккаунтами: политика смотрит на общую
    историю продавца, независимо от того, какой аккаунт писал. 'never' —
    повторов нет никогда (по умолчанию); 'after_days' — кампания фоллоу-апа
    по истечении срока. Пустая/кривая дата прошлого контакта при 'never'
    всё равно запрещает: нет данных — пишем без разрешения нельзя.
    """
    if policy == "after_days":
        try:
            prev = datetime.fromisoformat(prior_sent_at)
        except (TypeError, ValueError):
            return False, "дата прошлого контакта не читается — повтор запрещён"
        wait = timedelta(days=max(int(after_days), 0))
        if now - prev >= wait:
            return True, ""
        left_days = max((wait - (now - prev)).days, 0)
        return False, (f"прошлый контакт {prev.date()}, повтор возможен "
                       f"через {left_days} дн. (после {after_days})")
    return False, f"повторные контакты запрещены политикой ({policy})"


def raise_if_platform_restricted(driver, extra_markers: tuple = (), stage: str = "") -> None:
    """Бросает PlatformRestricted, если на странице маркеры ограничения.

    Общая точка для всех мест, где sender показывает Ozon страницу: стартовая
    проверка входа, переподключение, каждая открытая карточка продавца.
    С собой несёт текст страницы — из него Risk Control вытаскивает срок
    снятия лимита, если платформа его назвала.
    """
    from ..crm.risk import PlatformRestricted, page_text, match_risk_markers

    text = page_text(driver)
    marker = match_risk_markers(text, extra_markers)
    if marker:
        raise PlatformRestricted(marker=marker, snippet=text, stage=stage)


def probe_platform(driver, extra_markers: tuple = (), attempts: int = 3,
                   pause_seconds: int = 15, on_progress=None) -> Tuple[bool, str]:
    """Чекает платформу до N перезагрузок, НЕ трогая очередь.

    Правило оператора после ночи 02–03.10.2026 (аккаунт ушёл в антифрод,
    прогон сожгли 104 продавца): при антибот-челлендже или «подозрительной
    активности» — до 3 перезагрузок, ни одной отправки и ни одной пометки
    «не отправлено»; не отпустило — полный стоп. Возвращает (отпустило,
    последний маркер).
    """
    from ..crm.risk import scan_driver

    attempts = max(int(attempts), 1)
    marker = ""
    for attempt in range(1, attempts + 1):
        try:
            driver.get("https://www.ozon.ru/")
        except Exception as exc:                         # noqa: BLE001
            if on_progress:
                on_progress(f"Проверка {attempt}/{attempts}: страница не открылась "
                            f"({str(exc)[:80]})")
            time.sleep(pause_seconds)
            continue
        time.sleep(5)
        marker = scan_driver(driver, extra_markers)
        if not marker:
            if on_progress and attempt > 1:
                on_progress(f"Проверка {attempt}/{attempts}: платформа отпустила — продолжаю")
            return True, ""
        if on_progress:
            on_progress(f"Проверка {attempt}/{attempts}: маркер «{marker}» — "
                        "перезагрузка без отправки")
        time.sleep(pause_seconds)
    return False, marker


def probe_or_raise(driver, extra_markers: tuple = (), stage: str = "",
                   on_progress=None) -> None:
    """Маркер ограничения → до 3 перезагрузок без отправки; не отпустило — стоп.

    Отличие от raise_if_platform_restricted: одноразовый маркер (моргнувший
    челлендж) не роняет прогон — сначала чекает probe_platform. Продавец,
    на котором пойман маркер, НЕ получает «не отправлено»: он остаётся в
    статусе «в работе» и вернётся в очередь через release_stale.
    """
    from ..crm.risk import scan_driver, page_text

    marker = scan_driver(driver, extra_markers)
    if not marker:
        return
    ok, last_marker = probe_platform(driver, extra_markers, on_progress=on_progress)
    if ok:
        return
    raise PlatformRestricted(marker=marker or last_marker,
                             snippet=page_text(driver), stage=stage)


# Когда вкладку отправщика закрывают (вместе с окном Chrome или отдельно),
# chromedriver отвечает такими ошибками. Это не вина продавца: после
# переподключения к браузеру его можно отправить без списания попытки.
_BROWSER_LOST_MARKERS = (
    "no such window",
    "target window already closed",
    "web view not found",
    "invalid session id",
    "chrome not reachable",
)


def _browser_lost(exc: Exception) -> bool:
    """Потерялось ли окно/сессия браузера, а не контент страницы."""
    text = str(exc).lower()
    return any(marker in text for marker in _BROWSER_LOST_MARKERS)


class NoChatAvailable(Exception):
    """У продавца нельзя открыть чат: личка закрыта или чат не открылся.

    Это не сбой отправки — продавец не получит сообщение ни при каких
    повторах. Такие записи уходят в «пропущен» сразу, чтобы очередь шла
    дальше, а причина остаётся в last_error.
    """


# Точные тексты заглушек Ozon для невозможности написать продавцу (скрины
# оператора от 03.10.2026). Совпадение — скип «личка закрыта» БЕЗ счётчиков
# и стопов: у этого продавца закрыт чат, у остальных всё работает. Тексты
# сворачиваются по пробелам: заголовок на странице перенесён на две строки.
CLOSED_DM_MARKERS = (
    "этому продавцу пока нельзя написать",
    "этому продавцу нельзя написать",
    "чат заблокирован",
)


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

        # Поддерживаем две формы: "text|Написать" и "text=Написать".
        # Вторая не разбиралась вообще: строка целиком уходила как CSS-селектор
        # и chromedriver отклонял её как некорректную — из-за этого кнопка
        # «Написать» не находилась, и первая отправка падала.
        if "|" in selector:
            by, value = selector.split("|", 1)
        elif "=" in selector and selector.split("=", 1)[0] in ("css", "xpath", "text"):
            by, value = selector.split("=", 1)
        else:
            by, value = "css", selector
        mapping = {
            "css": By.CSS_SELECTOR,
            "xpath": By.XPATH,
            "text": By.XPATH,  # text=Условия -> //*[contains(text(), ...)]
        }
        if by == "text":
            # ensure_ascii=False обязателен: json.dumps по умолчанию превращает
            # кириллицу в \uXXXX, а XPath таких escape-последовательностей не
            # понимает и отклоняет селектор как некорректный. Из-за этого
            # «Написать» не находилось никогда.
            value = (
                "//*[contains(normalize-space(text()), "
                + json.dumps(value, ensure_ascii=False)
                + ")]"
            )
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
        if bool(self._find(self.selectors.login_marker)):
            return True
        try:
            links = self.driver.find_elements("css selector", "a[href*='/my/main'], a[href*='/my/orderlist']")
            if links:
                return True
        except Exception:
            pass
        try:
            cookie_names = {c.get("name") for c in self.driver.get_cookies()}
            if "__Secure-user-id" in cookie_names or "ozonIdAuthResponseToken" in cookie_names:
                return True
        except Exception:
            pass
        return False

    def open_chat(self, seller_id: str) -> None:
        """Открывает чат с продавцом через его страницу.

        Схема снята с живой страницы Ozon: на странице магазина ровно одна
        кнопка «Написать», она ведёт в чат с этим продавцом. Прямая ссылка на
        чат требует подписанного токена `url_sign`, который генерируется на
        клиенте при нажатии, поэтому воспроизводим именно нажатие.

        Раньше открывалась карточка товара и искалась кнопка «Чат» — теперь
        товар не нужен, достаточно seller_id.

        Если кнопки нет — у продавца закрыты личные сообщения: это не сбой,
        повторами не лечится, кидаем NoChatAvailable, чтобы очередь шла дальше.
        Поле ввода, не появившееся после клика, пробуем второй раз: чат иногда
        не раскрывается с первого открытия страницы.
        """
        url = f"https://www.ozon.ru/seller/{seller_id}/"
        for attempt in (1, 2):
            self._focus()
            try:
                # Если висит системный диалог/алерт — закрываем его
                try:
                    alert = self.driver.switch_to.alert
                    alert.accept()
                except Exception:
                    pass
                self.driver.get(url)
            except Exception:
                self._focus()
                try:
                    self.driver.get(url)
                except Exception:
                    pass
            self._focus()

            # Проверяем, что браузер реально перешёл на Ozon, а не остался на дашборде
            current_url = getattr(self.driver, "current_url", "") or ""
            if "ozon.ru" not in current_url:
                if attempt == 2:
                    raise RuntimeError(
                        f"Браузер не перешёл на Ozon (текущий URL: {current_url}) — переход сорвался"
                    )
                time.sleep(2)
                continue

            # Имитация человеческого поведения: просмотр страницы, скролл, изучение
            from ..utils.human_cursor import human_browse_page, human_click
            human_browse_page(self.driver, min_seconds=3.0, max_seconds=6.0)

            button = None
            deadline = time.time() + 8
            while time.time() < deadline:
                found = self._find(self.selectors.chat_button)
                if found:
                    button = found[0]
                    break
                time.sleep(0.5)
            if button is None:
                # Если страница не Ozon или не страница магазина — это сбой перехода, а не закрытая личка продавца!
                current_url = getattr(self.driver, "current_url", "") or ""
                if "ozon.ru" not in current_url or ("/seller/" not in current_url and str(seller_id) not in current_url):
                    raise RuntimeError(
                        f"Страница продавца не загрузилась (URL: {current_url}) — повторим позже"
                    )
                raise NoChatAvailable(
                    "нет кнопки «Написать» — личные сообщения закрыты"
                )

            time.sleep(random.uniform(0.4, 0.8))
            try:
                human_click(self.driver, button)
            except Exception:
                # Кнопку может перекрывать другой элемент — жмём через JS
                self.driver.execute_script("arguments[0].click();", button)

            # Ждём поле ввода: значит чат действительно открылся.
            # Раньше тут было 25 секунд × 2 попытки — на продавце с мёртвым
            # виджетом чата прогон стоял почти минуту. 10 секунд достаточно:
            # живой чат раскрывается за 2–5 секунд.
            try:
                self.wait_for(
                    self.selectors.message_input,
                    timeout=10,
                )
                return
            except TimeoutException:
                # Заглушки «Этому продавцу (пока) нельзя написать» и «Чат
                # заблокирован» — личные сообщения закрыты у САМОГО продавца.
                # Это не блок аккаунта и не сбой виджета: скип без счётчиков,
                # повторов и стопов (правило оператора от 03.10.2026).
                # Проверяем с первой же попытки — ждать второй у закрытой
                # лички смысла нет. Текст на странице перенесён на две строки
                # («…пока» / «нельзя написать»), поэтому сравниваем со
                # СВЁРНУТЫМИ пробелами — иначе совпадения не бывает
                # (проверено на продавце 200503).
                from ..crm.risk import page_text
                page = " ".join(page_text(self.driver).split()).lower()
                hit = next((m for m in CLOSED_DM_MARKERS if m in page), "")
                if hit:
                    raise NoChatAvailable(f"личка закрыта: {hit}")
                if attempt == 2:
                    raise NoChatAvailable(
                        "чат не открылся: поле ввода не появилось"
                    )
                time.sleep(2)

    def send(self, text: str) -> None:
        """Печатает текст и нажимает отправку.

        Кнопка отправки заблокирована, пока поле пустое, и разблокируется после
        ввода — проверено на живой странице.
        """
        from ..utils.human_cursor import human_click
        self._focus()
        field = self.wait_for(self.selectors.message_input)
        try:
            human_click(self.driver, field)
        except Exception:
            # Поле иногда перекрыто шапкой чата — обычный клик перехватывается.
            # Клик через JS ставит фокус так же, перехват не страшен.
            self.driver.execute_script("arguments[0].click();", field)

        self._set_field_value(field, text)
        time.sleep(random.uniform(0.5, 1.0))
        sent_clicked = False
        try:
            button = self.wait_for(self.selectors.send_button, timeout=5)
            try:
                human_click(self.driver, button)
                sent_clicked = True
            except Exception:
                self.driver.execute_script("arguments[0].click();", button)
                sent_clicked = True
        except Exception:
            pass

        if not sent_clicked:
            # Fallback: нажимаем Enter в поле ввода
            try:
                from selenium.webdriver.common.keys import Keys
                field.send_keys(Keys.ENTER)
            except Exception:
                pass

    def _clear_field(self, field) -> None:
        """Очищает поле ввода полностью и надёжно: комбинацией клавиш, JS-сеттером React и проверкой."""
        from selenium.webdriver.common.keys import Keys
        # 1. Нативное выделение и удаление через клавиатуру (Ctrl+A / Cmd+A + Backspace)
        try:
            field.click()
            time.sleep(0.2)
            field.send_keys(Keys.CONTROL, "a")
            field.send_keys(Keys.COMMAND, "a")
            field.send_keys(Keys.BACKSPACE)
            time.sleep(0.3)
        except Exception:
            pass

        # 2. React prototype setter для сброса внутреннего состояния инпута Ozon
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                el.focus();
                const proto = el.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) {
                    desc.set.call(el, '');
                } else {
                    el.value = '';
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                field,
            )
            time.sleep(0.3)
        except Exception:
            pass

        # 3. Контрольная проверка: если что-то осталось, выбиваем Backspace посимвольно
        try:
            val = str(field.get_attribute("value") or "").strip()
            if val:
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(Keys.BACKSPACE)
                time.sleep(0.2)
                for _ in range(len(val) + 5):
                    field.send_keys(Keys.BACKSPACE)
        except Exception:
            pass

    def _set_field_value(self, field, text: str) -> None:
        """Очищает поле и вводит новый текст по 1 символу в секунду (эмуляция действий человека)."""
        from selenium.webdriver.common.keys import Keys
        import random

        # Полная гарантированная очистка перед вводом
        self._clear_field(field)
        time.sleep(random.uniform(0.6, 1.2))

        lines = text.split("\n")
        for index, line in enumerate(lines):
            for char in line:
                field.send_keys(char)
                # Раз в секунду с естественным микро-разбросом человека (0.85 - 1.15 сек)
                time.sleep(random.uniform(0.85, 1.15))
            
            # Вместо \n (который сразу отправит сообщение), нажимаем Shift+Enter
            if index < len(lines) - 1:
                field.send_keys(Keys.SHIFT + Keys.ENTER)
                time.sleep(random.uniform(1.2, 2.0))

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

    def read_incoming_texts(self) -> List[str]:
        """Тексты сообщений СОБЕСЕДНИКА из открытого чата, в порядке очерёдности.

        Разметка снята с живого чата 26.09.2026: у каждого сообщения есть
        атрибут is-mine («mine» — наше, пустой — собеседник или система Ozon),
        а сам текст лежит внутри блока, чей класс содержит «mess». Время
        отправки — span с чч:мм, его вырезаем. Служебные сообщения Ozon
        («Ассистент Ozon…») тоже приходят с пустым is-mine — их отфильтровывает
        filter_incoming_bubbles в inbox.py, иначе робот принял бы подсказку
        системы за ответ продавца.
        """
        script = """
const isTime = (el) => /^\\d{1,2}:\\d{2}$/.test((el.textContent || '').trim());
return [...document.querySelectorAll('[is-mine]')]
  .filter((b) => b.getAttribute('is-mine') !== 'mine')
  .map((b) => {
    const clone = b.cloneNode(true);
    clone.querySelectorAll('span, svg').forEach((el) => {
      if (el.tagName.toLowerCase() === 'svg' || isTime(el)) { el.remove(); }
    });
    return (clone.innerText || clone.textContent || '').trim();
  })
  .filter(Boolean);
"""
        self._focus()
        return [str(t) for t in (self.driver.execute_script(script) or []) if str(t).strip()]

    def has_existing_dialog(self) -> bool:
        """Проверяет, есть ли в открытом чате уже отправленные или входящие сообщения."""
        self._focus()
        try:
            # Ищем любые пузыри сообщений в DOM чата Ozon
            count = self.driver.execute_script("""
                return document.querySelectorAll('[is-mine], [class*="messageBubble"], [class*="chat-message"], [data-widget="chatMessage"]').length;
            """)
            return bool(count and int(count) > 0)
        except Exception:
            return False


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

    # Дневной лимит: у аккаунта свой счётчик — свои сообщения за сегодня
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


def login_flow(data_dir: Path, timeout_seconds: int = 300, on_progress=None,
               account: Optional[SenderAccount] = None) -> bool:
    """Открывает Chrome с профилем аккаунта, чтобы вы вошли в Ozon.

    Профиль парсера (`~/.ozon_parser_chrome` для main, свой для остальных)
    отделён от вашего обычного браузера: сессия из него не переносится.
    Поэтому вход нужно сделать один раз именно в этом окне — дальше cookies
    сохранятся и отправка будет работать без входа. У каждого аккаунта
    свой профиль: залогиниться в нескольких = выполнить login для каждого.

    Функция ждёт, пока вход действительно произойдёт, и только потом отдаёт
    управление: иначе отправка упала бы на первом же продавце.
    """
    from ..utils.selenium_manager import SeleniumManager

    def say(message: str) -> None:
        logger.info(message)
        if on_progress:
            on_progress(message)

    if account is not None:
        manager = SeleniumManager(
            headless=False,
            profile_dir=account.profile_dir(),
            debug_port=account.debug_port(),
            proxy=getattr(account, "proxy", ""),
            account_id=account.id,
        )
        say(f"Аккаунт {account.id}: профиль {account.profile_dir()}, "
            f"порт {account.debug_port()}" + (f", прокси: {account.proxy}" if getattr(account, "proxy", "") else ""))
    else:
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


def check_login_live(data_dir: Path, on_progress=None,
                     account: Optional[SenderAccount] = None) -> dict:
    """Живая проверка входа: открывает браузер и смотрит, узнаёт ли Ozon сессию.

    Без браузера вход проверить нельзя, поэтому это отдельная операция, а не
    часть обычной проверки готовности: она открывает окно и занимает время.
    Проверяется сессия КОНКРЕТНОГО аккаунта: у остальных менеджеров своя.
    """
    from ..utils.selenium_manager import SeleniumManager

    result = {"logged_in": False, "error": ""}
    manager = None
    try:
        if account is not None:
            manager = SeleniumManager(
                headless=False,
                profile_dir=account.profile_dir(),
                debug_port=account.debug_port(),
                proxy=getattr(account, "proxy", ""),
                account_id=account.id,
            )
        else:
            manager = SeleniumManager(headless=False)
        driver = manager.create_driver()
        driver.get("https://www.ozon.ru/")
        time.sleep(3)
        selectors = ChatSelectors.load(data_dir / "selectors.json")
        client = ChatClient(driver, selectors, manager=manager)
        result["logged_in"] = bool(client.is_logged_in())
        if not result["logged_in"]:
            who = f" --account {account.id}" if account is not None else ""
            result["error"] = (
                "Ozon не узнал сессию. Выполните: python run_outreach.py login" + who
            )
    except Exception as exc:                                 # noqa: BLE001
        result["error"] = str(exc)[:200]
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:                                # noqa: BLE001
                pass
    if on_progress:
        on_progress("Вход подтверждён" if result["logged_in"] else f"Вход не подтверждён: {result['error']}")
    return result


def build_driver(headless: bool = False, account: Optional[SenderAccount] = None):
    """Драйвер с постоянным профилем — в нём живёт сессия Ozon аккаунта.

    Возвращаем и менеджер: через него закрываем только свою вкладку (а не весь
    Chrome) и переключаемся на неё перед каждой операцией. С аккаунтом —
    свой Chrome: свой профиль (сессия аккаунта) и свой порт отладки.
    """
    from ..utils.selenium_manager import SeleniumManager

    if account is not None:
        manager = SeleniumManager(
            headless=headless,
            profile_dir=account.profile_dir(),
            debug_port=account.debug_port(),
            proxy=getattr(account, "proxy", ""),
            account_id=account.id,
        )
    else:
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
    account: Optional[SenderAccount] = None,
) -> dict:
    """Прогоняет очередь с соблюдением ограничителей.

    `template` — либо один `MessageTemplate`, либо `TemplatePool` с несколькими
    текстами: тогда каждому следующему продавцу уходит тот, что использовался
    реже остальных, и подряд идущие сообщения не повторяются.

    `account` — аккаунт отправителя (менеджера): свой Chrome-профиль, свой
    дневной лимит и свой стоп-кран. None — основной аккаунт main. Несколько
    аккаунтов = несколько процессов, очередь в базе размечает продавцов
    атомарно, дублей не бывает.

    Возвращает сводку: сколько отправлено, сколько неудач, почему остановились.
    Если `once=True` — делает не больше одной отправки и выходит (удобно для
    ручной проверки, что всё настроено верно).
    """
    report = {"sent": 0, "failed": 0, "skipped": 0, "no_chat": 0, "stopped_reason": "",
              "dry_run": config.dry_run}
    # Одна сессия (решение оператора 03.10.2026: мультиаккаунт выведен из
    # эксплуатации): стоп-кран один глобальный, лимит считается по всем
    # сообщениям — как до мультиаккаунта. Метка аккаунта в журнале остаётся
    # (sender_account_id из настроек), чтобы история продавцов не терялась.
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

    def todays_cap(now: datetime) -> int:
        """Действующий дневной лимит на сегодня: разгон свежего аккаунта,
        суточный джиттер и авто-замедление по откликам (effective_daily_cap).
        Считается на каждом витке: на границе суток лимит меняется сам."""
        ramp_started = (ramp_start_date(data_dir, now.date())
                        if config.ramp_days > 0 else None)
        feedback = (queue.outreach_feedback(days=7)
                    if config.adaptive_slowdown and hasattr(queue, "outreach_feedback")
                    else None)
        return effective_daily_cap(config, now, feedback=feedback,
                                   ramp_started=ramp_started)

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
    # «Чат не открылся» — временный сбой: продавец ничего не получил, и через
    # час его можно пробовать снова. Без возврата такие продавцы копились в
    # «ошибках» навсегда, хотя виноват виджет Ozon, а не продавец.
    if hasattr(queue, "requeue_transient_chat_failures"):
        requeued = queue.requeue_transient_chat_failures(older_than_minutes=60)
        if requeued:
            progress(f"Вернул в очередь {requeued} продавцов после временных ошибок чата")

    # Один шаблон — частный случай пула: дальше код не различает, сколько
    # текстов в каталоге, и ротация работает всегда.
    if not isinstance(template, TemplatePool):
        template = TemplatePool([template])
    elif config.allowed_templates:
        # Фильтруем пул шаблонов только до разрешённых (например, только тексты с анализом)
        filtered = [t for t in template if t.name in config.allowed_templates]
        if filtered:
            template = TemplatePool(filtered)
        else:
            logger.warning(
                f"Ни один шаблон не совпал с allowed_templates={config.allowed_templates}. "
                f"Используем все доступные ({[t.name for t in template]})."
            )

    def next_message(row) -> Tuple[str, str, str]:
        """Текст, имя шаблона и experiment_id (если продавец в эксперименте).

        Эксперимент главнее ротации: продавцу, назначенному в вариант, уходит
        РОВНО назначенный текст — иначе сравнение групп теряет смысл. Вне
        экспериментов работает обычная раскладка (наименее использованный +
        A/B-ранжирование по ответам).
        """
        experiment = (queue.experiment_for_seller(row["seller_id"])
                      if hasattr(queue, "experiment_for_seller") else None)
        if experiment and hasattr(template, "get"):
            chosen = template.get(experiment["variant"])
            if chosen is not None and not chosen.is_placeholder():
                return (chosen.render(dict(row)), chosen.name,
                        str(experiment["experiment_id"]))
        usage = queue.template_usage() if hasattr(queue, "template_usage") else {}
        last = queue.last_template_used() if hasattr(queue, "last_template_used") else ""
        perf = queue.template_performance() if hasattr(queue, "template_performance") else {}
        chosen = template.pick(usage, avoid=last, performance=perf or None)
        return chosen.render(dict(row)), chosen.name, ""

    driver = None
    manager = None
    selectors = None
    client = None
    current_seller = ""
    extra_risk_markers = load_extra_markers(data_dir)

    def trigger_risk(exc: PlatformRestricted) -> None:
        """Risk Control: стоп-кран файлом + инцидент в базу + событие в UI.

        Дальнейшее — дело оператора: проверить причину в браузере и снять
        паузу явно (resume). Автопродолжения нет и не предусматривается.
        """
        until_at = ""
        try:
            from ..crm.risk import parse_resume_time
            until_at = parse_resume_time(exc.snippet)
        except Exception:                                  # noqa: BLE001
            pass
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text(
            f"risk {now_fn().isoformat(timespec='seconds')} "
            f"marker={exc.marker} stage={exc.stage} seller={current_seller} "
            f"until={until_at or 'неизвестно'}\n",
            encoding="utf-8",
        )
        if hasattr(queue, "record_risk_incident"):
            try:
                queue.record_risk_incident(
                    marker=exc.marker, stage=exc.stage,
                    seller_id=current_seller,
                    detail=exc.snippet,
                    until_at=until_at,
                )
            except Exception:                              # noqa: BLE001
                logger.warning("Не записал инцидент риска в базу", exc_info=True)
        emit("risk", marker=exc.marker, stage=exc.stage, seller_id=current_seller)
        msg_text = (
            f"⚠️ ВНИМАНИЕ: СТОП — ограничение платформы!\n\n"
            f"Маркер: «{exc.marker}»\n"
            f"Этап: {exc.stage or 'неизвестен'}\n\n"
            "Отправка остановлена, стоп-кран установлен, инцидент записан.\n"
            + (f"Платформа разрешит отправку после {until_at}. "
               "Сторож снимет стоп-кран сам в этот срок."
               if until_at else
               "Срок снятия платформа не назвала — дальше только вручную (/resume).")
        )
        progress(msg_text)
        try:
            from ..notify.telegram import send as tg_send
            tg_send(msg_text)
        except Exception as tg_exc:
            logger.warning(f"Failed to send tg notification: {tg_exc}")

    def rebuild_browser() -> None:
        """Пересоздаёт подключение, если вкладку отправщика закрыли.

        Прогон идёт с паузами по 10 минут, и Chrome на этой машине общий:
        вкладку отправщика можно закрыть случайно или вместе с окном. Раньше
        каждая следующая попытка падала с «no such window: target window
        already closed», и очередь сгорала по одному продавцу за круг.
        Теперь переподключаемся и пробуем того же продавца ещё раз.
        """
        nonlocal manager, driver, client
        if manager is not None:
            try:
                manager.close()
            except Exception:                           # noqa: BLE001
                pass
        manager, driver = build_driver(headless=False, account=account)
        client = ChatClient(driver, selectors, manager=manager)
        client._focus()
        driver.get("https://www.ozon.ru/")
        time.sleep(3)
        probe_or_raise(driver, extra_risk_markers, stage="переподключение",
                       on_progress=progress)
        if not client.is_logged_in():
            raise RuntimeError(
                "После переподключения сессия Ozon не найдена — "
                "войдите в открывшемся окне и запустите снова"
            )

    try:
        if not config.dry_run:
            manager, driver = build_driver(headless=False, account=account)
            selectors = ChatSelectors.load(data_dir / "selectors.json")
            missing = selectors.missing()
            if missing:
                raise RuntimeError(f"Не заполнены селекторы: {', '.join(missing)}")
            client = ChatClient(driver, selectors, manager=manager)

            # Метка отправителя попадает в журнал (кто писал) и в защиту
            # от повторных контактов между разными аккаунтами.
            if hasattr(queue, "register_sender_account"):
                queue.register_sender_account(config.sender_account_id)

            # Сначала открываем Ozon. Драйвер создаёт НОВУЮ пустую вкладку
            # (about:blank), и проверка входа на ней всегда давала «сессии нет»,
            # даже когда сессия на месте — из-за этого отправка не запускалась.
            client._focus()
            driver.get("https://www.ozon.ru/")
            time.sleep(3)
            probe_or_raise(driver, extra_risk_markers, stage="проверка входа",
                           on_progress=progress)
            if not client.is_logged_in():
                raise RuntimeError(
                    "В браузере нет залогиненной сессии Ozon. "
                    "Войдите в аккаунт в открывшемся окне и запустите снова."
                )

            # Поведенческий прогрев аккаунта (имитация действий живого покупателя раз в сутки)
            try:
                from .warmup import run_buyer_warmup, should_warmup_today
                acc_name = config.sender_account_id or "main"
                if should_warmup_today(data_dir, acc_name):
                    run_buyer_warmup(driver, data_dir, account_id=acc_name, on_progress=progress)
            except Exception as warm_exc:  # noqa: BLE001
                logger.debug(f"Warmup error ignored: {warm_exc}")

        consecutive_failures = 0
        consecutive_no_chat = 0
        transient_streak = 0
        while True:
            if cancelled():
                if kill_switch_active(stop_file):
                    report["stopped_reason"] = "сработал стоп-кран"
                else:
                    report["stopped_reason"] = "остановлено вручную"
                progress(report["stopped_reason"])
                break

            decision = should_send_now(now_fn(), config, queue.sent_today(),
                                       next_allowed_at,
                                       effective_cap=todays_cap(now_fn()))
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
                    verified_only=True,
                    statuses=[STATUS_NEW],
                    min_orders=config.min_orders,
                    max_orders=config.max_orders,
                    min_reviews=config.min_reviews,
                    max_reviews=config.max_reviews,
                    limit=1,
                    offset=0,
                ) if hasattr(queue, "list_sellers") else []
                if not candidates:
                    report["stopped_reason"] = "очередь пуста"
                    progress(report["stopped_reason"])
                    break
                row = candidates[0]
                shop = row["shop_name"] or row["seller_id"]
                message, template_name, _experiment_id = next_message(row)
                progress(f"[предпросмотр] {shop} — кому: {row['sample_product_url']}")
                progress(f"[предпросмотр] текст ({template_name}):\n{message}")
                emit("preview", seller_id=row["seller_id"], shop=shop,
                     message=message, template=template_name)
                report["stopped_reason"] = "режим предпросмотра"
                report["preview"] = {"seller_id": row["seller_id"], "shop": shop,
                                     "message": message, "template": template_name}
                break

            row = queue.claim_next(
                order_fresh=config.fresh_first,
                min_orders=config.min_orders,
                max_orders=config.max_orders,
                min_reviews=config.min_reviews,
                max_reviews=config.max_reviews,
            )
            if not row:
                if config.loop and not once:
                    # Режим «по кругу»: пустая очередь — не остановка, а пауза.
                    # Продавцы приходят сбором, интервалы всё равно соблюдаются,
                    # поэтому проверяем пополнение раз в 5 минут.
                    progress("Очередь пуста — режим «по кругу»: жду пополнения "
                             "(проверю через 5 мин)")
                    emit("waiting", reason="очередь пуста — жду пополнения (по кругу)",
                         seconds=300)
                    rest(300)
                    continue
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
            current_seller = seller_id
            shop = row["shop_name"] or seller_id
            message, template_name, experiment_id = next_message(row)

            # Глобальная защита от дублей между аккаунтами. Статуса продавца
            # мало: его мог сбросить оператор (requeue). Смотрим в ОБЩУЮ
            # историю сообщений — писал ли кто угодно этому продавцу.
            prior = queue.prior_contact(seller_id) if hasattr(queue, "prior_contact") else None
            if prior is not None:
                allowed, reason = repeat_contact_allowed(
                    config.repeat_contact_policy, config.repeat_after_days,
                    prior["sent_at"], now_fn())
                if not allowed:
                    skip_reason = (
                        f"уже был контакт "
                        f"({prior['sender_account_id'] or 'аккаунт неизвестен'} "
                        f"{prior['sent_at'][:10]}): {reason}")
                    queue.mark_skipped(seller_id, skip_reason)
                    if hasattr(queue, "log_message"):
                        queue.log_message(seller_id, message, "skipped", skip_reason,
                                          template_name=template_name,
                                          sender_account_id=config.sender_account_id)
                    report["skipped"] += 1
                    emit("skipped", seller_id=seller_id, shop=shop, error=skip_reason)
                    progress(f"Пропущен {shop}: {skip_reason}")
                    continue

            emit("sending", seller_id=seller_id, shop=shop, template=template_name)

            # Попытки браузера для одного продавца: основная + одна после
            # переподключения, если вкладку закрыли. Это отдельно от attempts
            # в базе: потеря окна — не вина продавца, списывать его попытку
            # из-за неё нельзя.
            browser_attempts = 0
            sent_this_round = False
            while browser_attempts < 2:
                browser_attempts += 1
                try:
                    client.open_chat(row["seller_id"])
                    # Чат открыт — страница продавца уже в браузере: последний
                    # рубеж, на котором Ozon мог подставить заглушку вместо
                    # магазина. Дальше печатать текст в неизвестно что нельзя.
                    # Маркер → 3 перезагрузки без отправки; не отпустило — стоп.
                    probe_or_raise(
                        driver, extra_risk_markers, stage=f"открытие чата {seller_id}",
                        on_progress=progress)
                    client.send(message)
                    if client.confirm_sent():
                        queue.mark_sent(seller_id, message, template_name=template_name,
                                        sender_account_id=config.sender_account_id,
                                        experiment_id=experiment_id)
                        report["sent"] += 1
                        consecutive_failures = 0
                        consecutive_no_chat = 0
                        transient_streak = 0
                        sent_this_round = True
                        emit("sent", seller_id=seller_id, shop=shop,
                             total=report["sent"], template=template_name)
                        break
                    queue.mark_failed(seller_id, "не подтвердилась отправка", retryable=True)
                    if hasattr(queue, "log_message"):
                        queue.log_message(seller_id, message, "failed",
                                          "не подтвердилась отправка",
                                          template_name=template_name)
                    report["failed"] += 1
                    consecutive_failures += 1
                    emit("failed", seller_id=seller_id, shop=shop, error="не подтвердилась отправка")
                    break
                except NoChatAvailable as exc:
                    # Три разных случая под одним исключением:
                    # * «нет кнопки Написать» и «личка закрыта: этому продавцу
                    #   пока нельзя написать» — закрытые личные сообщения
                    #   самого продавца: скип и дальше, без счётчиков и
                    #   порогов, стопов не бывает;
                    # * «чат не открылся: поле ввода не появилось» — сбой
                    #   виджета ИЛИ тихий антифрод: продавец остаётся без
                    #   пометки об ошибке и возвращается в очередь сам
                    #   (release_stale); после 3 подряд — чек платформы
                    #   (3 перезагрузки) и стоп при ограничении.
                    transient = "чат не открылся" in str(exc)
                    if transient:
                        consecutive_no_chat += 1
                        transient_streak += 1
                        # БЕЗ пометки «не отправлено» (правило оператора после
                        # ночи 02–03.10.2026): вино может быть не продавца, а
                        # ограничения платформы. Продавец остаётся «в работе»
                        # и через release_stale (30 мин) вернётся в очередь
                        # сам; ни статуса failed, ни записи в журнале.
                        report["no_chat"] = report.get("no_chat", 0) + 1
                        progress(f"Чат не открылся ({shop}) — вернётся в очередь "
                                 "через 30 мин, без пометки об ошибке")
                        emit("no_chat", seller_id=seller_id, shop=shop, error=str(exc)[:200])
                    else:
                        queue.mark_skipped(seller_id, str(exc))
                        if hasattr(queue, "log_message"):
                            queue.log_message(seller_id, message, "skipped", str(exc),
                                              template_name=template_name)
                        report["skipped"] += 1
                        progress(f"Пропущен {shop}: {exc} — вкладка идёт к следующему")
                        emit("skipped", seller_id=seller_id, shop=shop, error=str(exc)[:200])
                    break
                except PlatformRestricted:
                    # Ограничение платформы обрабатывает внешний обработчик:
                    # остановка всего прогона, а не переход к следующему.
                    raise
                except Exception as exc:                   # noqa: BLE001
                    if _browser_lost(exc) and browser_attempts < 2:
                        progress(
                            f"Вкладка отправщика закрылась ({str(exc)[:100]}) — "
                            "переподключаюсь и повторяю"
                        )
                        try:
                            rebuild_browser()
                            continue
                        except Exception as reb_exc:       # noqa: BLE001
                            exc = reb_exc
                    retryable = int(row["attempts"] or 0) < config.max_attempts
                    queue.mark_failed(seller_id, str(exc), retryable=retryable)
                    if hasattr(queue, "log_message"):
                        queue.log_message(seller_id, message, "failed", str(exc),
                                          template_name=template_name)
                    report["failed"] += 1
                    consecutive_failures += 1
                    emit("failed", seller_id=seller_id, shop=shop, error=str(exc)[:200])
                    break

            # Backoff при серии сбоев: техническая авария не должна долбить
            # очередь каждые interval минут. Это пауза, а не обход ограничений:
            # при признаках ограничения платформы сработал бы риск-стоп выше.
            backoff = backoff_seconds(consecutive_failures,
                                      config.backoff_base_minutes,
                                      config.backoff_max_minutes)
            if backoff:
                progress(f"Сбоев подряд: {consecutive_failures} — "
                         f"техническая пауза {backoff // 60} мин")
            if consecutive_failures >= config.max_consecutive_failures:
                report["stopped_reason"] = (
                    f"остановлено: {consecutive_failures} сбоев подряд — "
                    "посмотрите журнал ошибок, нужен человек")
                emit("stopped", reason=report["stopped_reason"])
                progress(report["stopped_reason"])
                break
            # Серия «чат не открылся» — так выглядит тихий антифрод: аккаунт
            # залогинен, но платформа закрыла ему чаты (ночь 02–03.10.2026:
            # 103 продавца сожжены, потому что предохранитель видел живую
            # сессию и продолжал). По правилу оператора: 3 перезагрузки без
            # единой отправки, не отпустило — полный стоп. Продавцы этой серии
            # помечены retryable-ошибкой и вернутся в очередь через час.
            if consecutive_no_chat >= 3:
                progress("3+ подряд чат не открылся — чекаю платформу: "
                         "3 перезагрузки без отправки")
                ok, marker = probe_platform(driver, extra_risk_markers,
                                            on_progress=progress)
                if not ok:
                    from ..crm.risk import page_text
                    raise PlatformRestricted(
                        marker=marker,
                        snippet=page_text(driver),
                        stage="серия чатов не открылась",
                    )
                # Маркеров нет — это НЕ ограничение платформы: моргает виджет
                # или Ozon тормозит (утро 03.10.2026: чаты с проверенными
                # продавцами открываются, аккаунт жив, а прогон умирал на
                # каждом витке). Продолжаем с паузой: очередь не страдает
                # (пометок «не отправлено» нет), а при настоящем ограничении
                # маркеры появятся на следующей серии и сработает ветка выше.
                progress("Ограничений нет — чаты моргнули, продолжаю "
                         "с паузой 10 минут")
                rest(600)
                consecutive_no_chat = 0

            # Полная пауза — только после реальной отправки. Если сообщение
            # НЕ ушло (чат закрыт, сбой виджета, ошибка), очередь не
            # сдвинулась: короткий перерыв и к следующему. У серии сбоев
            # чата пауза растёт (30 с → 5 мин потолок): идём до конца, но
            # не пулемётом по мёртвому виджету. Серийный backoff при этом
            # сильнее обеих: техническая авария не должна долбить очередь.
            if sent_this_round:
                delay = max(plan_next_delay(config), backoff)
            else:
                cooldown = min(30 * 2 ** (transient_streak - 2), 300) \
                    if transient_streak >= 2 else 0
                delay = max(failed_round_pause_seconds(), cooldown, backoff)
                wait_txt = (f"{delay} сек" if delay < 60 else f"{delay // 60} мин")
                progress(f"Сообщение не ушло — пауза {wait_txt} и вкладка "
                         "идёт к следующему продавцу")
            next_allowed_at = now_fn() + timedelta(seconds=delay)

            if once:
                report["stopped_reason"] = "режим одной отправки"
                break
    except PlatformRestricted as exc:
        # Risk Control: ограничение платформы останавливает ВЕСЬ прогон —
        # и с этапа проверки входа, и из середины цикла. Стоп-кран, запись
        # инцидента в базу и событие в интерфейс делает trigger_risk.
        trigger_risk(exc)
        report["stopped_reason"] = f"ограничение платформы: {exc.marker}"
        report["risk"] = {"marker": exc.marker, "stage": exc.stage,
                          "seller_id": current_seller}
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
