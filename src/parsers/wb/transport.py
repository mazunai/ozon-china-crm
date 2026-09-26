"""HTTP-транспорт к Wildberries: curl_cffi с отпечатком Chrome.

WB фильтрует клиентов по TLS-отпечатку: живая проверка 26.09.2026 — requests
получает 403 на search.wb.ru, curl_cffi с impersonate="chrome" проходит на
card.wb.ru. Интерфейс транспорта намеренно не зависит от curl_cffi: если
завтра понадобится браузерный клиент (рассылка, сложные челленджи), он
подключается тем же контрактом get_json().

Исключения:
    WBBlocked         — маркетплейс закрыл доступ (403/челлендж): это не баг
                        парсера, а состояние, которое UI показывает честно
                        («нужен прокси»);
    WBEndpointMissing — все версии эндпоинта не отвечают (404) — API переехал;
    WBTransportError  — сеть/таймауты/5xx после ретраев.
"""

import json
import logging
import os
import time
from typing import Callable, Dict, Optional

from .endpoints import (
    CARD_URL,
    CARD_VERSIONS,
    SELLER_INFO_URLS,
    SEARCH_URL,
    SEARCH_VERSIONS,
    DEFAULT_DEST,
)

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as curl_requests
except ImportError:                                      # pragma: no cover
    curl_requests = None

# Маркеры антибот-страницы WB («Подозрительная активность. Пожалуйста,
# подождите» — снимок живого челленджа 26.09.2026).
_CHALLENGE_MARKERS = ("подозрительная активность", "challenge", "captcha")


class WBBlocked(Exception):
    """Доступ закрыт антиботом — нужен другой IP (прокси) или пауза."""


class WBEndpointMissing(Exception):
    """Все известные версии эндпоинта ответили 404 — API переехал."""


class WBTransportError(Exception):
    """Сетевая проблема после ретраев: таймаут, соединение, 5xx."""


def wb_proxy() -> str:
    """Прокси для WB из окружения. Пусто — ходим с IP машины напрямую."""
    return (os.environ.get("WB_PROXY") or "").strip()


def wb_dest() -> int:
    """Гео-регион запросов (влияет на цены/остатки). По умолчанию Москва."""
    raw = (os.environ.get("WB_DEST") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_DEST
    except ValueError:
        return DEFAULT_DEST


def wb_pace() -> float:
    """Пауза между запросами, сек. WB жёстче Ozon по частоте — не ускоряться."""
    raw = (os.environ.get("WB_PACE") or "").strip().replace(",", ".")
    try:
        value = float(raw) if raw else 1.2
    except ValueError:
        value = 1.2
    return max(0.3, value)


class WBHttpClient:
    """Один клиент на прогон: TLS-отпечаток Chrome + ретраи + кэш версий.

    Рабочая версия эндпоинта запоминается на клиенте, поэтому перебор версий
    происходит один раз за прогон, а не на каждый запрос.
    """

    MAX_ATTEMPTS = 3
    RETRY_DELAYS = (2.0, 4.0)

    def __init__(self, proxy: str = "", timeout: float = 20.0):
        if curl_requests is None:
            raise WBTransportError(
                "Не установлен curl_cffi: pip install curl_cffi "
                "(обычный requests WB закрывает по TLS-отпечатку)"
            )
        self.proxy = proxy or wb_proxy()
        self.timeout = timeout
        self.requests = 0
        self._working: Dict[str, str] = {}   # вид запроса → рабочая версия

    def _raw_get(self, url: str, params: Optional[dict]):
        """Один GET без ретраев. Возвращает (status, text)."""
        headers = {
            "Accept": "*/*",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }
        session = curl_requests.Session(impersonate="chrome", timeout=self.timeout)
        try:
            response = session.get(
                url, params=params, headers=headers,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            )
            self.requests += 1
            return response.status_code, response.text
        finally:
            try:
                session.close()
            except Exception:                            # noqa: BLE001
                pass

    @staticmethod
    def _raise_for_block(status: int, body: str) -> None:
        if status == 200:
            return
        lowered = (body or "")[:2000].lower()
        challenged = any(marker in lowered for marker in _CHALLENGE_MARKERS)
        # 403/429/498 — защита WB; 503 у WB тоже обычно «остынь».
        if status in (403, 429, 498, 503) or challenged:
            raise WBBlocked(f"WB закрыл доступ (HTTP {status}) — нужен прокси или пауза")

    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        """GET с ретраями. 200 → dict; блокировка/404/сеть — свои исключения."""
        last_error: Optional[Exception] = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                status, body = self._raw_get(url, params)
                self._raise_for_block(status, body)
                if status == 200:
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError:
                        raise WBTransportError(
                            f"WB отдал не-JSON (200) на {url}: {body[:120]!r}"
                        )
                if status == 404:
                    raise WBEndpointMissing(f"HTTP 404 на {url}")
                last_error = WBTransportError(f"HTTP {status} на {url}")
            except (WBBlocked, WBEndpointMissing):
                raise                                       # ретраить бессмысленно
            except Exception as exc:                     # noqa: BLE001
                last_error = WBTransportError(f"Сеть: {exc}")

            if attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)])

        raise last_error or WBTransportError(f"Неизвестная ошибка запроса {url}")

    def _get_with_versions(self, kind: str, url_pattern: str, versions: tuple,
                           params_factory: Callable[[str], dict]) -> dict:
        """Перебирает версии эндпоинта, начиная с запомненной рабочей.

        При переезде API парсер сам находит новую версию без правки кода —
        за это отвечают списки версий в endpoints.py.
        """
        order = list(versions)
        working = self._working.get(kind)
        if working in order:
            order.insert(0, order.pop(order.index(working)))

        last: Optional[Exception] = None
        for version in order:
            try:
                payload = self.get_json(url_pattern.format(version=version),
                                        params_factory(version))
                self._working[kind] = version
                return payload
            except WBEndpointMissing as exc:
                last = exc
                continue
        self._working.pop(kind, None)
        raise WBEndpointMissing(str(last))

    # --- Эндпоинты --------------------------------------------------------

    def search(self, query: str, page: int = 1, limit: int = 100, sort: str = "popular") -> dict:
        """Выдача поиска/категории (товары содержат supplierId — карточка отдельно
        не нужна, это главный выигрыш против Ozon, где продавца ищут по карточке)."""
        params = {
            "appType": 1, "curr": "rub", "dest": wb_dest(),
            "query": query, "resultset": "catalog",
            "page": page, "limit": limit, "sort": sort,
            "ab_testing": "false",
        }
        return self._get_with_versions(
            "search", SEARCH_URL, SEARCH_VERSIONS, lambda _v: params
        )

    def product_cards(self, nm_ids) -> dict:
        """Батч карточек по артикулам (nm=1;2;3). Задел под витрины продавцов."""
        params = {
            "appType": 1, "curr": "rub", "dest": wb_dest(), "spp": 30,
            "nm": ";".join(str(nm) for nm in nm_ids),
        }
        return self._get_with_versions(
            "card", CARD_URL, CARD_VERSIONS, lambda _v: params
        )

    def supplier_info(self, supplier_id) -> Optional[dict]:
        """Реквизиты продавца. None — все кандидаты ответили 404 (API переехал).

        Это единственный источник страны продавца: без него классификация
        невозможна, поэтому caller обязан честно пропустить продавца.
        """
        for pattern in SELLER_INFO_URLS:
            url = pattern.format(supplier_id=supplier_id)
            try:
                return self.get_json(url)
            except WBEndpointMissing:
                continue
        return None
