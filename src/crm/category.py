"""Категория товара из карточки Ozon — источник нишевой градации продавцов.

Как устроено в карточке (снято с живого API 27.09.2026, см. AGENTS.md §3.7):

* В карточке есть виджет `breadCrumbs-*` — хлебные крошки с полным путём:
  «Красота и здоровье > Макияж > Глаза > Тени». Каждый элемент несёт text и
  link вида `/category/<slug>-<id>/`.
* Последняя крошка может быть страницей БРЕНДА, а не категории: у неё путь
  длиннее — `/category/<slug-категории>/<slug-бренда>-<id>/` (два сегмента).
  Категорийная крошка всегда одно-сегментная — по этому признаку и отсеиваем.
* Прямого поля «category» в карточке нет, а список `/category/` в других
  виджетах — это глобальное меню сайта (одинаково для всех карточек),
  его брать нельзя.

Модуль чистый: без сети и базы — чтобы тесты и сбор считали одинаково.
"""

import json
import re
from typing import Dict, Optional

# Категорийная крошка: один сегмент пути, оканчивается на -<число>
_CATEGORY_LINK_RE = re.compile(r"category/([a-z0-9\-]+)-(\d+)")


def extract_category(card_json: Optional[Dict]) -> Optional[Dict]:
    """Достаёт путь категории из хлебных крошек карточки товара.

    Возвращает {path, leaf, category_id, depth} или None, если крошек нет
    (карточка без категории, битый ответ). Path — через « > », leaf —
    последняя категорийная крошка, category_id — её числовой id.
    """
    for key, raw in (card_json or {}).get("widgetStates", {}).items():
        if not key.startswith("breadCrumbs") or not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        crumbs = []
        for crumb in payload.get("breadcrumbs") or []:
            link = (crumb.get("link") or "").strip("/")
            text = (crumb.get("text") or "").strip()
            match = _CATEGORY_LINK_RE.fullmatch(link)
            if match and text:
                crumbs.append({"text": text, "id": match.group(2)})
        if not crumbs:
            continue

        leaf = crumbs[-1]
        return {
            "path": " > ".join(c["text"] for c in crumbs),
            "leaf": leaf["text"],
            "category_id": leaf["id"],
            "depth": len(crumbs),
        }
    return None
