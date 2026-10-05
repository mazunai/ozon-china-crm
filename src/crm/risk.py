"""Risk Control: обнаружение ограничений и блокировок платформы.

Принцип: если Ozon сообщает о подозрительной активности, капче или
ограничении — отправка немедленно останавливается, инцидент фиксируется в
базе, оператору уходит уведомление. НИКАКИХ обходов не существует и не будет:
ни второй аккаунт, ни другой профиль браузера, ни подмена отпечатка. Снять
паузу может только оператор после проверки причины (см. resume в дашборде
или /resume в Telegram).

Маркеры — это реальные тексты страниц Ozon (антибот-челлендж, заглушки
доступа). Список можно расширить файлом `outreach_data/risk_markers.json`
(JSON-массив дополнительных подстрок) — без правки кода, если Ozon сменит
формулировки.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Все сравнения — в нижнем регистре по подстроке. Ложные срабатывания на
# обычной странице магазина дают только фразы целиком, поэтому маркеры —
# длинные осмысленные строки, а не отдельные слова.
DEFAULT_RISK_MARKERS = (
    # страница-челлендж Ozon: заголовок вкладки и текст «Antibot Challenge Page»
    "antibot challenge",
    # лимит на активность: Ozon показывает точную формулировку с датой снятия
    "подозрительную активность",
    "подозрительная активность",
    "заметили подозрительную",
    "новые сообщения можно будет отправить",
    "исходящую от вас",
    "доступ ограничен",
    "доступ временно ограничен",
    "подтвердите, что вы не робот",
    "подтвердите что вы не робот",
    "проверка на робота",
    "нам жаль, но доступ",
    "уважаемый пользователь, доступ",
    "подозрительный трафик",
    "попробуйте позже. доступ",
    "captcha",
    # Страница антифрода, притворяющаяся ошибкой сети (снято 02.10.2026):
    # «Похоже, нет соединения / Выключите VPN… / Инцидент: fab.chlg_…» —
    # это вердикт антибота Ozon, а не проблема интернета.
    "похоже, нет соединения",
    "выключите vpn, перезагрузите роутер",
    "fab.chlg_",
)

# Дополнительные маркеры читаются из файла на каждом вызове load_extra_markers
# (он — раз за прогон отправки; кэш здесь ловил «дописал маркер — не видно»)


def load_extra_markers(data_dir: Path) -> tuple:
    """Дополнительные маркеры из outreach_data/risk_markers.json (если есть).

    Файл факультативный: нет файла — работает встроенный список. Битый JSON
    не роняет отправку — пишем в лог и работаем без него. Читается заново на
    каждый вызов: файл маленький, вызовов за прогон немного, а кэш здесь
    превращался в баг «дописал маркер — система его не видит».
    """
    path = Path(data_dir) / "risk_markers.json"
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return tuple(str(m).strip().lower() for m in data if str(m).strip())
    except (ValueError, OSError) as exc:                     # noqa: PERF203
        logger.warning(f"Не прочёл {path.name}: {exc}")
    return ()


def match_risk_markers(text: str, extra_markers: tuple = ()) -> str:
    """Возвращает первый найденный маркер или '' — текст чистый.

    Пустой/короткий текст не считается чистым сам по себе: решение о риске
    принимает только наличие маркера. Отсутствие признаков логина — это
    отдельная проверка (is_logged_in), а не риск-сигнал.
    """
    lowered = (text or "").lower()
    if not lowered:
        return ""
    for marker in list(extra_markers) + list(DEFAULT_RISK_MARKERS):
        if marker in lowered:
            return marker
    return ""


def page_text(driver) -> str:
    """Заголовок вкладки + текст body — то, что сканируем на маркеры."""
    if driver is None:
        return ""
    try:
        parts = [str(driver.title or "")]
        body = driver.find_elements("css selector", "body")
        if body:
            parts.append(str(body[0].get_attribute("innerText") or ""))
        return " \n".join(parts)
    except Exception:                                        # noqa: BLE001
        return ""


def scan_driver(driver, extra_markers: tuple = ()) -> str:
    """Сканирует текущую страницу браузера на маркеры ограничения.

    Смотрим и заголовок вкладки (челлендж Ozon виден именно в нём), и текст
    body — заглушки бывают и там и там. Возвращает маркер или ''.
    Ошибка чтения страницы — не риск-сигнал: пусть решает обычная обработка
    ошибок отправки, а не этот скан.
    """
    return match_risk_markers(page_text(driver), extra_markers)


# Шаблон Ozon: «Новые сообщения можно будет отправить 2 октября после
# 12:43 по UTC». Платформа сама называет срок — мы его исполняем, а не
# обходим. Месяцы — префиксы слов («октябр» ловит «октября/октябре»).
_MONTH_PREFIXES = ("январ", "феврал", "март", "апрел", "мая", "июн",
                   "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
_RESUME_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(_MONTH_PREFIXES) + r")\w*\s+.*?(\d{1,2}):(\d{2})",
    re.IGNORECASE | re.DOTALL,
)


def parse_resume_time(text: str) -> str:
    """ISO-момент (UTC), когда платформа разрешит отправку; '' если не найден.

    Разбираем ТОЛЬКО явно названный платформой срок из текста страницы.
    Никакой эвристики «когда бы фортуну не дразнить» —执行的 ровно то,
    что сказано.
    """
    if not text:
        return ""
    match = _RESUME_RE.search(text)
    if not match:
        return ""
    day, month_prefix, hour, minute = (
        int(match.group(1)), match.group(2).lower(), int(match.group(3)), int(match.group(4)))
    month = next((i + 1 for i, prefix in enumerate(_MONTH_PREFIXES)
                  if month_prefix.startswith(prefix)), 0)
    if not month:
        return ""
    now = datetime.now(timezone.utc)
    candidate = now.replace(month=month, day=day, hour=hour, minute=minute,
                            second=0, microsecond=0)
    if candidate < now - timedelta(hours=1):
        # дата уже прошла — платформа имела в виду следующий год
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate.isoformat(timespec="seconds")


class PlatformRestricted(Exception):
    """Платформа сообщила об ограничении/подозрительной активности.

    Не для повторов и не для смены аккаунта: поймавший обязан остановить
    цикл отправки и зафиксировать инцидент (store.record_risk_incident).
    """

    def __init__(self, marker: str, snippet: str = "", stage: str = ""):
        super().__init__(
            f"Ограничение платформы: маркер «{marker}»"
            + (f", этап {stage}" if stage else "")
        )
        self.marker = marker
        self.snippet = (snippet or "")[:1500]
        self.stage = stage
