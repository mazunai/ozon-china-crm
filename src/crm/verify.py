"""
Подтверждение страны продавца вторым, независимым способом.

Уровни достоверности (см. `store.VERIFY_*`):

    uscc       — 18-значный код из блока реквизитов. Абсолютный признак.
    warehouse  — склад продавца в Китае. Виджет доставки товара отдаёт секцию
                 addressSelect со строкой «Со склада продавца, <регион>», и
                 если регион китайский, продавец везёт из Китая.
    heuristic  — эвристики по названию. НЕ доказательство, в дашборд по
                 умолчанию не попадает.

Почему проверка по складу вообще нужна: USCC есть не у всех — модалка отдаёт
реквизиты не для каждого продавца. Склад же видно почти всегда.

Как читается склад
------------------
Виджет доставки приходит пустым (`webDelivery: {}`) в обычном ответе карточки —
его состояние грузится отдельным POST-запросом. Нужен `asyncData` из узла
`layout` с `component == "webDelivery"`, без него эндпоинт отвечает 400.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Склад в Китае: пиньинь + Sheng (省, провинция) или Shi (市, город).
# Проверено вживую: Shandong Sheng, Zhejiang Sheng, Fujian Sheng,
# Guangdong Sheng, Jiangsu Sheng, Heilongjiang Sheng, Shanghai.
CN_WAREHOUSE_RE = re.compile(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s+(?:Sheng|Shi)\b")

# Российские регионы в той же строке. Кириллица — надёжный признак РФ.
RU_WAREHOUSE_RE = re.compile(r"[А-Яа-яЁё]")

WAREHOUSE_PREFIXES = ("Со склада продавца,", "Со склада Ozon,", "Со склада,")

# Проверенные китайские города, которые Ozon пишет без суффикса Sheng/Shi
CN_CITIES_WITHOUT_SUFFIX = {
    "shanghai", "beijing", "tianjin", "chongqing", "shenzhen", "guangzhou",
    "hangzhou", "nanjing", "suzhou", "xiamen", "qingdao", "ningbo", "yiwu",
}

WIDGET_STATE_URL = "/api/composer-api.bx/widget/json/v2"
PAGE_API_URL = "/api/composer-api.bx/page/json/v2"


@dataclass
class WarehouseInfo:
    is_china: bool = False
    region: str = ""
    raw: str = ""
    is_cross_border: bool = False        # найдена секция global_info
    reason: str = ""


def parse_warehouse_sections(state: dict) -> WarehouseInfo:
    """Разбирает состояние виджета доставки.

    `state` — распарсенный `state` из ответа виджетного API: там есть
    `sections[]`, среди которых `addressSelect` (адрес и склад) и, если
    доставка из-за рубежа, `global_info`.
    """
    info = WarehouseInfo()
    if not isinstance(state, dict):
        return info

    sections = state.get("sections") or []
    texts = []

    for section in sections:
        if not isinstance(section, dict):
            continue
        if section.get("type") == "global_info":
            info.is_cross_border = True
            info.reason = "есть секция global_info («Условия доставки из-за рубежа»)"

        for item in section.get("descriptionRs") or []:
            content = item.get("content") if isinstance(item, dict) else None
            if content:
                texts.append(str(content))

    for text in texts:
        for prefix in WAREHOUSE_PREFIXES:
            if not text.startswith(prefix):
                continue
            region = text[len(prefix):].strip()
            if not region:
                continue
            info.raw = text
            info.region = region
            if RU_WAREHOUSE_RE.search(region):
                info.is_china = False
                info.reason = f"склад в России: {region}"
            elif CN_WAREHOUSE_RE.search(region) or region.lower() in CN_CITIES_WITHOUT_SUFFIX:
                info.is_china = True
                info.reason = f"склад в Китае: {region}"
            else:
                # Латиница без явного китайского признака: бывает и Стамбул,
                # и польский Lower Silesian — поэтому не считаем Китаем.
                info.is_china = False
                info.reason = f"склад за рубежом, но не Китай: {region}"
            return info

    if info.is_cross_border and not info.region:
        info.reason = info.reason or "доставка из-за рубежа, склад не указан"
    return info


def find_delivery_node(layout) -> Optional[dict]:
    """Ищет в дереве layout узел виджета доставки."""
    found = None

    def walk(node):
        nonlocal found
        if found is not None or not node:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("component") == "webDelivery":
            found = node
            return
        for value in node.values():
            walk(value)

    walk(layout)
    return found


def build_widget_request(node: dict, product_path: str) -> Optional[dict]:
    """Собирает параметры POST-запроса состояния виджета.

    Возвращает None, если в узле нет asyncData — без него эндпоинт отвечает
    ошибкой 400, и запрос бессмысленен.
    """
    if not node or not node.get("stateId"):
        return None
    async_data = node.get("asyncData")
    if not async_data:
        return None
    return {
        "url": f"{WIDGET_STATE_URL}?widgetStateId={node['stateId']}",
        "body": {
            "asyncData": async_data,
            "extraBody": None,
            "url": product_path,
            "componentName": node.get("name") or "pdp.webDelivery",
        },
    }


def parse_state_envelope(raw: str) -> dict:
    """Достаёт `state` из ответа виджетного API."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    state = payload.get("state")
    return state if isinstance(state, dict) else {}


def verify_by_warehouse(raw_widget_response: str) -> WarehouseInfo:
    """Полный путь: сырой ответ виджета -> вывод о стране склада."""
    return parse_warehouse_sections(parse_state_envelope(raw_widget_response))
