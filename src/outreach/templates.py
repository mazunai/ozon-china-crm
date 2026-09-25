"""
Шаблоны сообщений для рассылки.

Шаблон пишет человек: маркетинговый текст зависит от услуги, которую вы
предлагаете, и сочинять его за вас здесь неуместно. Модуль отвечает только за
подстановку переменных и за проверку того, что шаблон вообще пригоден.

Переменные, доступные в тексте: {shop_name}, {company_name}, {orders},
{reviews}, {months}, {years}, {rating}, {province}, {seller_id}.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Dict, Optional

from .queue_db import OutreachQueue
from .scoring import parse_count, parse_rating

logger = logging.getLogger(__name__)

ALLOWED_FIELDS = {
    "shop_name", "company_name", "orders", "orders_word", "reviews", "reviews_word",
    "months", "years", "tenure", "rating", "province", "seller_id",
}

# Признаки строки «отписаться». Наличие такого пункта — нормальная практика
# для холодных писем и требование ст. 18 ФЗ «О рекламе» для рекламных.
OPTOUT_MARKERS = (
    "не пишите", "не беспокоить", "отписат", "отказ", "не интересно",
    "если не актуально", "больше не писать",
)

# Метка в файле-заготовке. Пока она в тексте, реальная отправка запрещена —
# иначе легко разослать по сотне магазинов шаблон, который вы не писали.
PLACEHOLDER_MARKER = "ЗАМЕНИТЕ ЭТОТ ТЕКСТ"


class TemplateError(ValueError):
    """Шаблон непригоден: отсутствует, пуст или содержит неизвестную переменную."""


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
    return sorted(p for p in directory.glob("*.txt") if p.is_file())


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
