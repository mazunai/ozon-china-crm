"""
Шаблоны сообщений для рассылки.

Шаблон пишет человек: маркетинговый текст зависит от услуги, которую вы
предлагаете, и сочинять его за вас здесь неуместно. Модуль отвечает только за
подстановку переменных и за проверку того, что шаблон вообще пригоден.

Шаблонов может быть много: `TemplatePool` держит весь каталог и отдаёт текст,
который использовался реже остальных, — чтобы подряд идущие продавцы получали
разные сообщения, а не одну и ту же копипасту.

Переменные, доступные в тексте: {shop_name}, {company_name}, {orders},
{reviews}, {months}, {years}, {rating}, {province}, {seller_id}.
"""

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Dict, List, Optional, Sequence

from .queue_db import OutreachQueue
from .scoring import parse_count, parse_rating

logger = logging.getLogger(__name__)

ALLOWED_FIELDS = {
    "shop_name", "company_name", "orders", "orders_word", "reviews", "reviews_word",
    "months", "years", "tenure", "rating", "province", "seller_id",
}

# Признаки строки «отписаться». Наличие такого пункта — нормальная практика
# для холодных писем и требование ст. 18 ФЗ «О рекламе» для рекламных.
# Китайские варианты — рассылка идет китайским продавцам, и отписка в тексте
# должна быть на языке получателя.
OPTOUT_MARKERS = (
    "не пишите", "не беспокоить", "отписат", "отказ", "не интересно",
    "если не актуально", "больше не писать",
    # 退订 — «отписаться», 不再打扰 — «больше не беспокоить» и т.д.
    "退订", "不再打扰", "不想再收到", "不感兴趣请", "请勿再发",
)

# Метка в файле-заготовке. Пока она в тексте, реальная отправка запрещена —
# иначе легко разослать по сотне магазинов шаблон, который вы не писали.
PLACEHOLDER_MARKER = "ЗАМЕНИТЕ ЭТОТ ТЕКСТ"

# Стандартная строка отписки для импортируемых наборов. Рассылка идёт
# китайским продавцам — строка на языке получателя; маркеры 退订/不再打扰
# выше её же и распознают при проверке.
DEFAULT_OPTOUT_LINE = "如不需要此类信息，请回复「退订」，我们不会再打扰您。"

# Заголовок варианта во вставленном наборе: «№1 — продажная возможность».
BULK_HEADER_RE = re.compile(r"^\s*№\s*(\d+)\s*[—–\-:]?\s*(.*)$")
# Строка-разделитель между вариантами: только тире, подчёркивания, знаки.
BULK_SEPARATOR_RE = re.compile(r"^[\s⸻—–\-_·•=~]*$")


class TemplateError(ValueError):
    """Шаблон непригоден: отсутствует, пуст или содержит неизвестную переменную."""


def ensure_optout(body: str) -> tuple:
    """Добавляет стандартную строку отписки, если её в тексте нет.

    Возвращает (текст, добавлена_ли_строка). Строка отписки — не украшение,
    а требование ст. 18 ФЗ «О рекламе» и предохранитель от кнопки «спам»:
    получателю нужен способ отказаться, кроме жалобы на аккаунт.
    """
    lowered = body.lower()
    if any(marker in lowered for marker in OPTOUT_MARKERS):
        return body, False
    return body.rstrip() + "\n" + DEFAULT_OPTOUT_LINE, True


@dataclass
class MessageTemplate:
    name: str
    body: str
    path: Optional[Path] = None

    def fields(self) -> set:
        """Какие переменные реально используются в шаблоне."""
        used = set()
        for _, field_name, _, _ in Formatter().parse(self.body):
            if field_name:
                # Отсекаем формат-спецификацию: {rating:.1f} -> rating
                used.add(field_name.split(".")[0].split("[")[0])
        return used

    def validate(self, require_optout: bool = True) -> None:
        if not self.body.strip():
            raise TemplateError(f"Шаблон '{self.name}' пуст")

        unknown = self.fields() - ALLOWED_FIELDS
        if unknown:
            raise TemplateError(
                f"Шаблон '{self.name}' использует неизвестные переменные: "
                f"{', '.join(sorted(unknown))}. Доступны: {', '.join(sorted(ALLOWED_FIELDS))}"
            )

        if require_optout:
            lowered = self.body.lower()
            if not any(marker in lowered for marker in OPTOUT_MARKERS):
                raise TemplateError(
                    f"В шаблоне '{self.name}' нет строки отказа от рассылки. "
                    "Добавьте фразу вида «Если не актуально — напишите, больше не буду писать». "
                    "Это и вежливо, и требуется законом для рекламных сообщений."
                )

    def is_placeholder(self) -> bool:
        """Шаблон ещё не отредактирован человеком."""
        return PLACEHOLDER_MARKER in self.body

    def render(self, row: Dict) -> str:
        """Подставляет данные продавца. Отсутствующие значения — пустая строка."""
        orders = parse_count(row.get("orders", 0))
        reviews = parse_count(row.get("reviews", 0))
        months = int(row.get("months_on_ozon", 0) or 0)
        rating = parse_rating(row.get("rating", 0))

        values = {
            "shop_name": row.get("shop_name") or "",
            "company_name": row.get("company_name") or "",
            "orders": _human_count(orders),
            "orders_word": _plural(orders, "заказ", "заказа", "заказов"),
            "reviews": _human_count(reviews),
            "reviews_word": _plural(reviews, "отзыв", "отзыва", "отзывов"),
            "months": months,
            "years": round(months / 12, 1),
            "tenure": format_tenure(months),
            "rating": round(rating, 1),
            "province": row.get("province") or "",
            "seller_id": row.get("seller_id") or "",
        }
        try:
            return self.body.format(**values)
        except (KeyError, IndexError) as exc:
            raise TemplateError(f"Не удалось подставить {exc} в шаблон '{self.name}'") from exc


def parse_template_set(text: str) -> List[MessageTemplate]:
    """Разбирает вставленный в интерфейсе набор текстов на варианты.

    Заголовок варианта — строка, начинающаяся с «№N», остаток строки —
    название. Строки-разделители (⸻, ---) и пустые строки в текст не
    попадают. Без единого заголовка это ошибка разметки, а не «один большой
    текст»: случайный склеенный абзац не должен уйти продавцам.

    Определена ПОСЛЕ класса MessageTemplate: аннотация возврата честная,
    а на Python < 3.14 она вычисляется в момент объявления функции.
    """
    blocks: List[List] = []  # [номер, название, строки]
    for raw in (text or "").splitlines():
        header = BULK_HEADER_RE.match(raw)
        if header:
            blocks.append([int(header.group(1)), header.group(2).strip(), []])
        elif blocks and not BULK_SEPARATOR_RE.match(raw):
            blocks[-1][2].append(raw.rstrip())
    if not blocks:
        raise TemplateError(
            "Не нашёл ни одного заголовка «№N — название». "
            "Каждый вариант должен начинаться строкой вида «№1 — продажная возможность»."
        )
    templates = []
    for num, title, lines in blocks:
        body = "\n".join(lines).strip()
        if not body:
            continue
        templates.append(MessageTemplate(name=title or f"вариант {num}", body=body))
    if not templates:
        raise TemplateError("Все варианты в наборе пустые — есть только заголовки.")
    return templates


def _human_count(value: int) -> str:
    """9369 -> «9 369». Число в текст лучше подставлять уже с разрядами."""
    return f"{value:,}".replace(",", " ")


def _plural(amount: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 год, 2 года, 5 лет, 11 лет."""
    tail = abs(amount) % 100
    if 11 <= tail <= 19:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def format_tenure(months: int) -> str:
    """Месяцы -> «1 год», «3 месяца», «4 года», «5 лет».

    Подставляет готовую фразу: без этого в тексте получалось «1.0 лет»,
    что сразу выдаёт автоматическую рассылку.
    """
    if months <= 0:
        return ""
    if months >= 12 and months % 12 == 0:
        years = months // 12
        return f"{years} {_plural(years, 'год', 'года', 'лет')}"
    return f"{months} {_plural(months, 'месяц', 'месяца', 'месяцев')}"


def load_template(path: Path, require_optout: bool = True) -> MessageTemplate:
    if not path.exists():
        raise TemplateError(
            f"Шаблон не найден: {path}\n"
            "Создайте файл с текстом сообщения. Без него отправка не запустится — "
            "это защита от случайной рассылки заготовки, которую вы не писали."
        )
    template = MessageTemplate(name=path.stem, body=path.read_text(encoding="utf-8"), path=path)
    template.validate(require_optout=require_optout)
    return template


def list_templates(directory: Path) -> list:
    if not directory.exists():
        return []
    paths = [p for p in directory.glob("*.txt") if p.is_file()]
    return sorted(paths, key=lambda p: _natural_key(p.stem))


def _natural_key(name: str) -> list:
    """Сортировка с числами внутри: «msg2» раньше «msg10», а не наоборот.

    Обычное сравнение строк ставит «msg10» между «msg1» и «msg2» — при
    тридцати нумерованных шаблонах список выглядит перемешанным, и непонятно,
    в каком порядке они пойдут по кругу.
    """
    parts = re.split(r"(\d+)", name.lower())
    # Элементы — пары с числом впереди, чтобы сравнение не падало на str vs int
    return [(1, int(p)) if p.isdigit() else (0, p) for p in parts]


class TemplatePool:
    """Каталог шаблонов, которые идут по кругу.

    Зачем пул. Один и тот же текст, отправленный подряд нескольким десяткам
    продавцов, читается как копипаста — и получателем, и площадкой. Пул отдаёт
    на каждое следующее сообщение текст, который использовался реже остальных,
    поэтому N шаблонов дают N разных сообщений N получателям.

    Поверх ротации работает A/B-ранжирование (см. `pick`): пока у варианта
    мало отправок, тексты расходятся поровну; когда статистика набрана,
    чаще уходит тот, на который продавцы отвечают чаще.

    Раскладка хранится не в памяти, а в журнале отправок: процесс можно
    перезапустить, ротация продолжится с того же места. Счётчики передаются
    в `pick` снаружи — пул не знает про базу и остаётся чистой логикой, которую
    легко покрыть тестами.
    """

    # A/B: сколько отправок набрать варианту, прежде чем ранжировать его по
    # ответам. Пока у варианта меньше — он крутится в общем заезде.
    AB_MIN_EXPOSURE = 5
    # Доля отправок «вслепую» и после набора статистики: без этого ранжирование
    # быстро замораживает победителя и перестаёт собирать данные.
    AB_EXPLORE_SHARE = 0.2
    # Сглаживание доли ответов: (ответы + PRIOR_REPLIES) / (отправки + PRIOR_SENDS).
    # Вариант с 0 ответов на 5 отправках не умирает мгновенно, а оценивается
    # скромно; с 3 ответами на 10 — уверенно лидирует.
    AB_PRIOR_REPLIES = 1.0
    AB_PRIOR_SENDS = 8.0

    def __init__(self, templates: Sequence[MessageTemplate]):
        if not templates:
            raise TemplateError("Пул шаблонов пуст — отправлять нечего")
        self._templates = sorted(templates, key=lambda t: _natural_key(t.name))
        self._by_name = {t.name: t for t in self._templates}

    @classmethod
    def load(cls, directory: Path, require_optout: bool = True) -> "TemplatePool":
        paths = list_templates(directory)
        if not paths:
            raise TemplateError(
                f"В {directory} нет ни одного шаблона.\n"
                "Скопируйте заготовку и напишите свой текст:\n"
                f"    cp {directory / 'example.txt.example'} {directory / 'main.txt'}"
            )
        return cls([load_template(p, require_optout=require_optout) for p in paths])

    def __len__(self) -> int:
        return len(self._templates)

    def __iter__(self):
        return iter(self._templates)

    @property
    def names(self) -> List[str]:
        return [t.name for t in self._templates]

    def get(self, name: str) -> Optional[MessageTemplate]:
        """Шаблон по имени. Нужен экспериментам: вариант там — имя файла,
        и он обязан уходить именно этот текст, а не «как решит ротация»."""
        return self._by_name.get(name)

    def usable(self) -> List[MessageTemplate]:
        """Тексты, готовые к отправке: заготовки с меткой не берём.

        Так можно держать в каталоге тридцать файлов и дописывать их по одному:
        недописанный текст просто не попадает в ротацию, а не блокирует рассылку.
        """
        return [t for t in self._templates if not t.is_placeholder()]

    @property
    def name(self) -> str:
        """Описание для интерфейса: «main.txt» для одного, «5 шаблонов» для пула."""
        if len(self._templates) == 1:
            return self._templates[0].name
        count = len(self._templates)
        return f"{count} {_plural(count, 'шаблон', 'шаблона', 'шаблонов')}"

    def validate(self) -> None:
        """Проверяет каждый шаблон пула. При загрузке они уже проверены."""
        for template in self._templates:
            template.validate()

    def is_placeholder(self) -> bool:
        """True, если отправлять нечего: все тексты — ещё недописанные заготовки."""
        return not self.usable()

    def pick(self, usage: Optional[Dict[str, int]] = None,
             avoid: str = "", performance: Optional[Dict[str, dict]] = None,
             min_exposure: Optional[int] = None, rng=None) -> MessageTemplate:
        """Следующий текст.

        Без `performance` — реже всего использованный (чистая ротация).
        С `performance` — A/B-ранжирование по доле ответов:

        1. Пока у варианта меньше `min_exposure` отправок, он участвует в
           общем заезде — так каждый вариант набирает статистику.
        2. Дальше примерно 1/5 отправок уходит «вслепую» (случайный вариант):
          排名 не замораживается, данные продолжают собираться.
        3. Остальные отправки получает вариант с лучшей долей ответов,
           посчитанной со сглаживанием — чтобы вариант с одним ответом на две
           отправки не побеждал проверенный 3/10 только из-за малой выборки.

        `usage` — сколько раз каждый шаблон уже ушёл (считается по журналу
        отправок). `avoid` — имя шаблона, который ушёл только что: его
        пропускаем, если есть из чего выбрать, чтобы два подряд идущих
        продавца не получили одинаковый текст.
        """
        usage = usage or {}
        rng = rng or random.Random()
        min_exposure = self.AB_MIN_EXPOSURE if min_exposure is None else min_exposure

        candidates = self.usable()
        if not candidates:
            raise TemplateError(
                "Все шаблоны — ещё заготовки (в тексте осталась метка "
                f"«{PLACEHOLDER_MARKER}»). Напишите хотя бы один текст."
            )

        if avoid and len(candidates) > 1:
            others = [t for t in candidates if t.name != avoid]
            if others:
                candidates = others

        if not performance:
            return min(candidates, key=lambda t: (usage.get(t.name, 0), _natural_key(t.name)))

        stats = {t.name: performance.get(t.name) or {"sent": 0, "replies": 0}
                 for t in candidates}

        # Фаза набора статистики: недоэкспонированные тексты крутятся в общем
        # порядке, рейтинг к ним пока не применяем.
        under = [t for t in candidates if stats[t.name]["sent"] < min_exposure]
        if under:
            return min(under, key=lambda t: (stats[t.name]["sent"], _natural_key(t.name)))

        # «Вслепую»: случайный вариант — рейтинг не должен застывать
        if rng.random() < self.AB_EXPLORE_SHARE:
            return rng.choice(sorted(candidates, key=lambda t: _natural_key(t.name)))

        # Эксплойт: лучший по доле ответов со сглаживанием; при равенстве —
        # тот, что реже уходил (данные по нему дороже).
        def rank(t: MessageTemplate):
            s = stats[t.name]
            rate = (s["replies"] + self.AB_PRIOR_REPLIES) / (s["sent"] + self.AB_PRIOR_SENDS)
            return (-rate, s["sent"], _natural_key(t.name))

        return min(candidates, key=rank)

    def usage_report(self, usage: Optional[Dict[str, int]] = None,
                     performance: Optional[Dict[str, dict]] = None) -> str:
        """Раскладка текстов. С performance — ранжированная таблица A/B."""
        if not self._templates:
            return "Шаблонов нет."
        if performance:
            return self._performance_report(performance)
        usage = usage or {}
        width = max(len(t.name) for t in self._templates)
        lines = []
        for t in self._templates:
            mark = "черновик" if t.is_placeholder() else f"{usage.get(t.name, 0)} раз"
            lines.append(f"  {t.name:<{width}}  {mark}")
        return "\n".join(lines)

    def _performance_report(self, performance: Dict[str, dict]) -> str:
        """Таблица рангов: отправки, ответы, доля. Лидер — сверху."""
        width = max(len(t.name) for t in self._templates)
        rows = []
        for t in self._templates:
            if t.is_placeholder():
                rows.append((None, t.name, "черновик"))
                continue
            s = performance.get(t.name) or {"sent": 0, "replies": 0}
            if s["sent"]:
                rate = (s["replies"] + self.AB_PRIOR_REPLIES) / (s["sent"] + self.AB_PRIOR_SENDS)
                rows.append((rate, t.name,
                             f"отправлено {s['sent']}, ответили {s['replies']}"
                             f" ({s['replies'] / s['sent']:.0%})"))
            else:
                rows.append((None, t.name, "ещё не отправлялся"))

        def _label(row):
            return (f"  {row[1]:<{width}}  {row[2]}"
                    + ("   ← лидер" if row[0] is not None and row[0] == max(
                        r[0] for r in rows if r[0] is not None) else ""))

        rated = sorted((r for r in rows if r[0] is not None),
                       key=lambda r: -r[0])
        unrated = [r for r in rows if r[0] is None]
        return "\n".join(_label(r) for r in rated + unrated)


def preview(template: MessageTemplate, queue: OutreachQueue, seller_id: Optional[str] = None) -> str:
    """Показывает готовый текст для одного продавца — проверить до рассылки."""
    if seller_id:
        rows = [r for r in queue.iter_sellers() if r["seller_id"] == seller_id]
    else:
        rows = [r for r in queue.iter_sellers(status="new") if r["sample_product_url"]][:1]

    if not rows:
        return "Нет подходящего продавца для предпросмотра."

    row = rows[0]
    header = f"— предпросмотр для {row['shop_name'] or row['seller_id']} —"
    return f"{header}\n{template.render(dict(row))}"
