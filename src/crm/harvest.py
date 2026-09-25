"""
Сбор китайских продавцов из раздела Ozon Global.

Как пользоваться: даёшь ссылку на раздел «Товары из Китая» — харвестер обходит
её страницы, достаёт продавцов, подтверждает страну по 18-значному коду из
реквизитов и складывает всё в базу.

Проверенные факты об этом разделе (живая разведка 25.09.2026)
--------------------------------------------------------------
* URL раздела:      /highlight/tovary-iz-kitaya-935133/
* API страницы:     /api/composer-api.bx/page/json/v2?url=<путь>&__rr=1
* Пагинация:        ?page=N, минимум до 100-й страницы, 8 товаров на страницу
* Товары раздела:   грид `tileGridDesktop-3669724-default-1` с params = "{}"

ГЛАВНАЯ ЛОВУШКА
---------------
На той же странице есть второй грид — с `"title":"Возможно, вам понравится"`
в params. Это рекомендации, и в них лежат обычные российские товары со складом
в Ленинградской области. Если взять «грид побольше» (в нём 30 позиций против 8),
получишь «китайский раздел» с российскими продавцами. Поэтому грид с непустыми
params отбрасывается: у настоящего листинга params строго `"{}"`.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .store import (
    VERIFY_HEURISTIC,
    VERIFY_NONE,
    VERIFY_USCC,
    VERIFY_WAREHOUSE,
    SellerStore,
)
from .verify import build_widget_request, find_delivery_node, verify_by_warehouse
from ..outreach.scoring import SellerMetrics, score_all
from ..parsers.chinese_filter import (
    classify_seller,
    extract_seller_registration,
    find_uscc,
    province_from_uscc,
)

logger = logging.getLogger(__name__)

PAGE_API = "/api/composer-api.bx/page/json/v2"
SELLER_MODAL = "/modal/shop-in-shop-info?seller_id={seller_id}"

# Грид настоящего листинга раздела: params пустые. Всё, что с title, —
# рекомендации («Возможно, вам понравится») с товарами не из раздела.
MAIN_GRID_PARAMS = "{}"

# Стартовая пауза между запросами. Ozon начинает притормаживать сессию на
# частых запросах (проверено: на ~150 запросах подряд запросы начали виснуть),
# поэтому темп сдерживаем осознанно. Пауза адаптивная: OzonPageClient сам
# поднимает её при медленных/битых ответах и плавно опускает к полу
# (delay_min), пока сессия отвечает чисто.
DEFAULT_DELAY = 0.35
DELAY_FLOOR = 0.3            # нижняя граница адаптивной паузы
DELAY_CEILING = 3.0          # верхняя граница при проблемах с сессией


@dataclass
class HarvestStats:
    pages: int = 0
    products: int = 0
    sellers_found: int = 0
    sellers_verified_uscc: int = 0
    sellers_verified_warehouse: int = 0
    sellers_rejected: int = 0
    # Известные продавцы, чьи модалки в этом прогоне не перечитывались
    # (метрики свежие) — и товары, взятые из кэша вместо карточек.
    sellers_skipped: int = 0
    cards_cached: int = 0
    requests: int = 0
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        extra = ""
        if self.sellers_skipped:
            extra += f", свежих пропущено={self.sellers_skipped}"
        if self.cards_cached:
            extra += f", карточек по кэшу={self.cards_cached}"
        return (
            f"страниц={self.pages}, товаров={self.products}, "
            f"продавцов={self.sellers_found} "
            f"(по USCC={self.sellers_verified_uscc}, по складу={self.sellers_verified_warehouse}, "
            f"отсеяно={self.sellers_rejected}){extra}, запросов={self.requests}"
        )


# Признаки антибот-челленджа. Проверено на VM: с датацентрового IP главная
# Ozon отдаёт челлендж (страница ~7 КБ), и fetch из-под такой страницы не
# работает. При этом сам API по прямому переходу отдаётся нормально — поэтому
# при челлендже переключаемся на навигацию.
CHALLENGE_MARKERS = (
    "challenge", "проверка браузера", "abt-challenge",
    "checking your browser", "antibot",
)
CHALLENGE_MAX_HTML = 20_000


class OzonPageClient:
    """Запросы к внутреннему API Ozon.

    Два способа доставки запроса, и выбор между ними важен:

    * `fetch` из контекста страницы — быстрый (десятые доли секунды), работает
      на домашнем IP, где страница Ozon открывается нормально;
    * навигация на URL API — медленнее (1-3 секунды), но работает всегда,
      включая случай, когда главная под антибот-челленджем: сам API при прямом
      переходе отдаётся без проверки.

    Режим `auto` начинает с быстрого способа и навсегда переключается на
    навигацию, если fetch не сработал или страница под челленджем.
    """

    def __init__(
        self,
        driver,
        delay: float = DEFAULT_DELAY,
        transport: str = "auto",
        adaptive: bool = True,
        min_delay: float = DELAY_FLOOR,
        max_delay: float = DELAY_CEILING,
    ):
        self.driver = driver
        self.delay = delay                    # текущая пауза (стартовая, потом адаптируется)
        self.adaptive = adaptive
        self.delay_min = min_delay
        self.delay_max = max_delay
        self.requests = 0
        self.last_error = ""
        self._origin_ready = False
        self._transport = transport          # auto | fetch | navigate
        self._fetch_failures = 0
        self._clean_streak = 0               # сколько запросов подряд без проблем

    def _page_is_challenged(self) -> bool:
        """Похожа ли текущая страница на антибот-челлендж."""
        try:
            source = (self.driver.page_source or "").lower()
        except Exception:                                # noqa: BLE001
            return False
        if not source:
            return False
        if len(source) > CHALLENGE_MAX_HTML:
            return False
        return any(marker in source for marker in CHALLENGE_MARKERS)

    def _use_fetch(self) -> bool:
        return self._transport in ("auto", "fetch") and self._fetch_failures == 0

    def _switch_to_navigation(self, reason: str) -> None:
        if self._transport != "navigate":
            logger.info(f"Перехожу на прямые запросы к API ({reason})")
        self._transport = "navigate"

    def ensure_ready(self) -> bool:
        """Гарантирует, что страница открыта на домене ozon.ru.

        Критично для запросов через fetch: с чужого origin (например `data:,`
        после неудачного подключения или отката на автоматизированный Chrome)
        браузер блокирует запрос к ozon.ru как кросс-доменный, и все ответы
        схлопываются в «Failed to fetch». Симптом обманчив — выглядит как
        «Ozon не отвечает», хотя дело в origin.
        """
        if self._origin_ready:
            return True
        try:
            current = self.driver.current_url or ""
        except Exception:
            current = ""

        if "ozon.ru" in current:
            self._origin_ready = True
            return True

        logger.info(f"Страница не на ozon.ru (сейчас {current[:60]!r}) — открываю сайт")
        try:
            self.driver.get("https://www.ozon.ru/")
            time.sleep(2)
            current = self.driver.current_url or ""
        except Exception as exc:                         # noqa: BLE001
            self.last_error = f"не удалось открыть ozon.ru: {exc}"
            logger.error(self.last_error)
            return False

        if "ozon.ru" not in current:
            self.last_error = (
                f"браузер не на ozon.ru (сейчас {current[:80]!r}). "
                "Похоже, не пройден антибот-челлендж: requests через fetch "
                "будут блокироваться как кросс-доменные."
            )
            logger.error(self.last_error)
            return False

        self._origin_ready = True
        return True

    def _fetch_raw(self, url: str, method: str, body: Optional[dict]) -> Optional[str]:
        """Забирает текст ответа через fetch в контексте страницы.

        Бэкенды разные, и у каждого свой способ дождаться ответа:
          * Selenium — `execute_async_script` с колбэком последним аргументом;
          * Camoufox (Playwright) — промис, который Playwright дожидается сам.
        Если ни одного способа нет, возвращаем None и работаем через навигацию.
        """
        payload = json.dumps(body) if body else None

        executor = getattr(self.driver, "execute_async_script", None)
        if executor is not None:
            script = f"""
            const cb = arguments[arguments.length - 1];
            fetch({json.dumps(url)}, {{
                method: {json.dumps(method)},
                headers: {{'accept': 'application/json, text/plain, */*',
                          'content-type': 'application/json'}},
                credentials: 'include',
                body: {json.dumps(payload)}
            }})
              .then(r => r.text())
              .then(t => cb(t))
              .catch(e => cb('ERR:' + e));
            """
            try:
                return executor(script)
            except Exception as exc:                 # noqa: BLE001
                logger.debug(f"execute_async_script не удался: {exc}")
                return None

        page = getattr(self.driver, "page", None)
        if page is not None:
            try:
                return page.evaluate(
                    """([url, method, payload]) => fetch(url, {
                         method: method,
                         headers: {'accept': 'application/json, text/plain, */*',
                                   'content-type': 'application/json'},
                         credentials: 'include',
                         body: payload === null ? undefined : payload
                       }).then(r => r.text()).catch(e => 'ERR:' + e)""",
                    [url, method, payload],
                )
            except Exception as exc:                 # noqa: BLE001
                logger.debug(f"page.evaluate не удался: {exc}")
                return None

        return None

    def fetch_json(self, path: str, method: str = "GET", body: Optional[dict] = None) -> Optional[dict]:
        """Забирает JSON по внутреннему пути Ozon (например `/api/...`)."""
        self.requests += 1

        # Без правильного origin fetch блокируется браузером, и мы получим
        # пустые ответы вместо понятной ошибки
        if not self.ensure_ready():
            return None

        url = path if path.startswith("http") else f"https://www.ozon.ru{path}"

        started = time.monotonic()
        raw = None
        if self._use_fetch():
            if self._page_is_challenged():
                self._switch_to_navigation("страница под антибот-челленджем")
            else:
                raw = self._fetch_raw(url, method, body)
                if raw is None:
                    self._fetch_failures += 1
                    self._switch_to_navigation("fetch не сработал")

        if raw is None:
            raw = self._fetch_by_navigation(url)

        data = None
        if not raw:
            self.last_error = f"пустой ответ на {path[:80]}"
        elif isinstance(raw, str) and raw.startswith("ERR:"):
            self.last_error = raw[:200]
            logger.debug(f"Ошибка fetch: {raw[:160]}")
        else:
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                self.last_error = f"не JSON в ответе на {path[:60]}: {exc}"

        self._adapt(time.monotonic() - started, isinstance(data, dict))
        if self.delay:
            time.sleep(self.delay)
        return data if isinstance(data, dict) else None

    def _adapt(self, elapsed: float, healthy: bool) -> None:
        """Подстраивает паузу под состояние сессии.

        Пока Ozon отвечает чисто и быстро — пауза плавно опускается к полу
        (экономим минуты на больших прогонах). Битый или медленный ответ —
        признак, что сессию притормаживают: пауза растёт с запасом, чтобы
        не усугубить. Навигационный транспорт не наказываем за «медленно»:
        там запрос сам по себе длится 1-3 секунды.
        """
        if not self.adaptive:
            return
        if not healthy:
            self._clean_streak = 0
            self.delay = min(self.delay * 2.0, self.delay_max)
            logger.info(f"Битый ответ — пауза поднята до {self.delay:.2f}с")
        elif self._transport != "navigate" and elapsed >= 2.0:
            self._clean_streak = 0
            self.delay = min(self.delay * 1.5, self.delay_max)
            logger.info(f"Медленный ответ ({elapsed:.1f}с) — пауза до {self.delay:.2f}с")
        else:
            self._clean_streak += 1
            if self._clean_streak >= 8 and self.delay > self.delay_min:
                self.delay = max(self.delay_min, self.delay * 0.9)
                self._clean_streak = 0

    def _page_text(self) -> str:
        """Текст текущей страницы.

        У Selenium и Camoufox разные способы: Camoufox (Playwright) принимает
        только функцию, а не строку с `return`. Поэтому ветвимся явно.
        """
        page = getattr(self.driver, "page", None)
        if page is not None:
            try:
                return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:                            # noqa: BLE001
                return ""
        try:
            return self.driver.execute_script(
                "return document.body ? document.body.innerText : ''"
            ) or ""
        except Exception:                                # noqa: BLE001
            return ""

    def _fetch_by_navigation(self, url: str) -> Optional[str]:
        """Медленный, но универсальный способ: открыть URL и прочитать JSON.

        Это основной путь на сервере: с датацентрового IP главная Ozon под
        антибот-челленджем, но сам API по прямому переходу отдаётся нормально.
        """
        try:
            self.driver.get(url)
        except Exception as exc:                         # noqa: BLE001
            self.last_error = f"навигация на {url[:70]} не удалась: {exc}"
            logger.debug(self.last_error)
            return None

        for _ in range(20):
            text = (self._page_text() or "").strip()
            if not text:
                try:
                    text = (self.driver.page_source or "").strip()
                except Exception:                        # noqa: BLE001
                    text = ""
            start = text.find("{")
            if start >= 0:
                candidate = text[start:]
                if "widgetStates" in candidate or '"state"' in candidate:
                    return candidate
            time.sleep(1)

        self.last_error = f"JSON не найден на странице {url[:70]}"
        return None

    def page(self, ozon_path: str) -> Optional[dict]:
        """Ответ страничного API для пути Ozon."""
        from urllib.parse import quote
        return self.fetch_json(f"{PAGE_API}?url={quote(ozon_path, safe='')}&__rr=1")

    def seller_modal(self, seller_id: str) -> Optional[dict]:
        return self.page(SELLER_MODAL.format(seller_id=seller_id))


# --- Разбор ответов -------------------------------------------------------


def extract_main_grid_items(widget_states: Dict, layout: Optional[object] = None) -> List[dict]:
    """Достаёт товары из главного листинга раздела, отбрасывая рекомендации.

    Ключевая проверка — params виджета. У настоящего листинга они равны `{}`
    (проверено на трёх страницах раздела), а у рекомендаций содержат `title`.
    """
    params_by_state: Dict[str, str] = {}
    layout = layout or []

    def walk(node):
        if not node:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        state_id = node.get("stateId")
        if state_id and node.get("component", "").startswith("tileGrid"):
            params_by_state[state_id] = str(node.get("params") or "")
        for value in node.values():
            walk(value)

    walk(layout)

    items: List[dict] = []
    for key, raw in (widget_states or {}).items():
        if not key.startswith("tileGrid") or not isinstance(raw, str):
            continue
        params = params_by_state.get(key)
        # Если узел не нашли в layout — считаем виджет рекомендательным и
        # пропускаем: лучше меньше товаров, чем мусор из другого раздела.
        if params is None or params.strip() != MAIN_GRID_PARAMS:
            logger.debug(f"Пропускаю грид {key}: params={params!r} (похоже на рекомендации)")
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in payload.get("items") or []:
            if item.get("id"):
                items.append(item)
    return items


def _decode_escapes(raw: str) -> str:
    """Раскодирует \\uXXXX-экранировки в строке ответа.

    Ozon отдаёт кириллицу и иероглифы в ASCII-экранированном виде
    (`\\u0417\\u0430\\u043a\\u0430\\u0437\\u043e\\u0432` вместо «Заказов»),
    поэтому искать текстовые метки в сырой строке бессмысленно — сначала
    раскодируем.

    Меняем только сами последовательности \\uXXXX. Вариант через
    `decode("unicode_escape")` здесь не годится: он трактует строку как
    Latin-1 и вместе с экранировками съедает кириллицу, превращая
    «Заказов» в пустую строку.
    """
    if not raw or "\\u" not in raw:
        return raw
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)


def extract_seller_from_card(widget_states: Dict) -> Tuple[str, str]:
    """Достаёт seller_id и название магазина из карточки товара."""
    seller_id, shop_name = "", ""

    for key, raw in (widget_states or {}).items():
        if not isinstance(raw, str):
            continue
        if key.startswith("webStickyProducts"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seller = payload.get("seller") or {}
            shop_name = seller.get("name") or shop_name
            match = re.search(r"/seller/(?:[^/\"]*?)(\d+)/?$", seller.get("link") or "")
            if match:
                seller_id = match.group(1)
        elif key.startswith("webCurrentSeller") and not seller_id:
            match = re.search(r'"sellerId"\s*:\s*"?(\d+)"?', raw) or re.search(r"/seller/(\d+)", raw)
            if match:
                seller_id = match.group(1)
            # Название магазина лежит в sellerCell, а не в header: в header
            # стоит служебный заголовок «Магазин», который к продавцу не относится.
            if not shop_name:
                decoded = _decode_escapes(raw)
                cell = re.search(r'"sellerCell".{0,400}?"title"\s*:\s*\{\s*"text"\s*:\s*"([^"]{2,60})"',
                                 decoded, re.DOTALL)
                if cell:
                    shop_name = cell.group(1)

    return seller_id, shop_name


def extract_card_layout(page_json: Dict) -> Optional[object]:
    return page_json.get("layout")


def parse_seller_modal(modal_json: Dict) -> dict:
    """Разбирает модалку продавца: реквизиты, метрики, страну.

    Возвращает словарь с company/uscc/province/metrics/classification.
    """
    result = {
        "company_name": "", "inn": "", "address": "", "uscc": "", "province": "",
        "registration_kind": "unknown", "orders_count": "", "reviews_count": "",
        "working_time": "", "average_rating": "", "registration_block": "",
    }
    states = (modal_json or {}).get("widgetStates") or {}

    # Блок реквизитов: textBlock со строками через <br>, где последняя строка —
    # регистрационный номер (18-значный USCC у китайцев, ОГРНИП/ИНН у россиян).
    candidates = []
    for key, raw in states.items():
        if not key.startswith("textBlock") or not isinstance(raw, str) or "<br>" not in raw:
            continue
        for text in re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
            decoded = text
            if "\\u" in decoded:
                try:
                    decoded = decoded.encode().decode("unicode_escape", errors="ignore")
                except Exception:
                    pass
            if "<br>" not in decoded:
                continue
            lines = [part.strip() for part in decoded.split("<br>") if part.strip()]
            if len(lines) < 2:
                continue
            tail = lines[-1]
            if find_uscc(tail) or re.fullmatch(r"\d{10,15}", tail):
                candidates.append((len(lines), len(decoded), decoded))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        parsed = extract_seller_registration(candidates[0][2])
        result["registration_block"] = candidates[0][2]
        result.update({
            "company_name": parsed.get("company", ""),
            "inn": parsed.get("registration", ""),
            "address": parsed.get("address", ""),
            "uscc": parsed.get("uscc", ""),
            "province": parsed.get("province", ""),
            "registration_kind": parsed.get("kind", "unknown"),
        })

    # Метрики лежат в cellList: пары «подпись → значение» отдельными атомами.
    # Кириллицу Ozon экранирует, поэтому сначала раскодируем строку.
    for key, raw in states.items():
        if not key.startswith("cellList") or not isinstance(raw, str):
            continue
        texts = re.findall(r'"text"\s*:\s*"([^"]{1,60})"', _decode_escapes(raw))
        for index, text in enumerate(texts):
            value = texts[index + 1] if index + 1 < len(texts) else ""
            lowered = text.strip().lower()
            if lowered == "заказов" and value:
                result["orders_count"] = value
            elif lowered in ("количество отзывов", "отзывов", "отзыв") and value:
                result["reviews_count"] = value
            elif lowered == "работает с ozon" and value:
                result["working_time"] = value
            elif lowered == "средняя оценка товаров" and value:
                result["average_rating"] = value
        break

    return result


# --- Сам харвестер --------------------------------------------------------


class ChinaSellerHarvester:
    """Обходит раздел Ozon Global и собирает подтверждённых китайских продавцов."""

    def __init__(
        self,
        client: OzonPageClient,
        store: Optional[SellerStore] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        stop_event: Optional["threading.Event"] = None,
        refresh_days: int = 7,
    ):
        self.client = client
        self.store = store
        self.on_progress = on_progress
        # Флаг отмены. Без него кнопка «Остановить» в дашборде была фиктивной:
        # событие выставлялось, но никто его не читал, и сбор шёл до конца.
        self.stop_event = stop_event
        # Структурный прогресс для интерфейса: (сделано, всего, фаза).
        # Отдельно от текстового лога, чтобы дашборд рисовал полосу, а не
        # вылавливал числа регулярками из сообщений.
        self.progress_cb = progress_cb
        # Как часто перечитывать модалки известных продавцов (метрики).
        # 0 — никогда не перечитывать, только новые.
        self.refresh_days = refresh_days
        self.stats = HarvestStats()
        self._seen_products: Set[str] = set()
        self._sellers: Dict[str, dict] = {}
        # Известные продавцы из базы: seller_id -> мета (для пропуска свежих
        # и имён магазинов при попадании в кэш товаров).
        self._known: Dict[str, dict] = {}

    def cancelled(self) -> bool:
        """Запрошена ли отмена. Проверяется в каждом цикле обхода."""
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _report(self, done: int, total: int, phase: str) -> None:
        if not self.progress_cb:
            return
        try:
            self.progress_cb(done, total, phase)
        except Exception as exc:                             # noqa: BLE001
            logger.debug(f"progress_cb упал: {exc}")

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.on_progress:
            self.on_progress(message)

    @staticmethod
    def path_from_url(url: str) -> str:
        """Превращает вставленную ссылку в путь для API.

        Принимает и полный URL, и голый путь — чтобы можно было вставить ссылку
        прямо из браузера.
        """
        match = re.search(r"ozon\.ru(/[^\s]*)", url)
        path = match.group(1) if match else (url if url.startswith("/") else f"/{url}")
        # Параметры вида ?page=2 сохраняем, а мусорные (at, sh, __rr) выкидываем
        if "?" in path:
            base, query = path.split("?", 1)
            keep = [
                part for part in query.split("&")
                if part.startswith("page=") or part.startswith("text=")
            ]
            path = base + ("?" + "&".join(keep) if keep else "")
        return path

    def collect_products(self, section_path: str, pages: int) -> List[str]:
        """Собирает ID товаров из главного листинга раздела."""
        product_ids: List[str] = []
        for page_number in range(1, max(1, pages) + 1):
            if self.cancelled():
                self.stats.cancelled = True
                self._log(f"Отменено на странице {page_number}")
                break
            path = section_path if page_number == 1 else f"{section_path}?page={page_number}"
            data = self.client.page(path)
            if not data:
                reason = getattr(self.client, "last_error", "") or "пустой ответ"
                self.stats.errors.append(f"страница {page_number}: {reason}")
                self._log(f"Страница {page_number} не получена: {reason}")
                if page_number == 1:
                    self._log(
                        "Первая страница не открылась — дальше идти бессмысленно. "
                        "Проверьте, что браузер прошёл антибот Ozon."
                    )
                break

            items = extract_main_grid_items(data.get("widgetStates") or {}, data.get("layout"))
            self.stats.pages += 1

            new = 0
            for item in items:
                article = str(item.get("id") or "")
                if article and article not in self._seen_products:
                    self._seen_products.add(article)
                    product_ids.append(article)
                    new += 1

            self._log(f"Страница {page_number}: товаров на странице {len(items)}, новых {new}")
            self._report(page_number, pages, "Собираю товары")
            if new == 0:
                self._log("Новых товаров нет — дальше страниц нет")
                break
        return product_ids

    def resolve_sellers(self, product_ids: Sequence[str]) -> Dict[str, dict]:
        """По товарам находит продавцов: карточка -> seller_id.

        Карточку качаем только у товаров, которых нет в кэше «товар →
        продавец»: на повторных проходах раздела почти всё берётся из кэша,
        и дорогой запрос карточки остаётся только новым товарам.
        """
        owners: Dict[str, str] = {}
        if self.store is not None:
            owners = self.store.product_owners(product_ids)

        sellers: Dict[str, dict] = {}
        for index, article in enumerate(product_ids, 1):
            if self.cancelled():
                self.stats.cancelled = True
                self._log(f"Отменено на товаре {index} из {len(product_ids)}")
                break

            cached = owners.get(article) if article in owners else None
            if cached and cached in self._known:
                self.stats.cards_cached += 1
                record = sellers.setdefault(cached, {
                    "shop_name": self._known[cached].get("shop_name", ""),
                    "products": [],
                    "delivery_node": None,
                })
                record["products"].append(article)
                self._report(index, len(product_ids), "Определяю продавцов")
                if index % 10 == 0:
                    self._log(f"Обработано товаров: {index}/{len(product_ids)}, "
                              f"продавцов {len(sellers)}")
                continue

            data = self.client.page(f"/product/{article}/")
            if not data:
                self.stats.errors.append(f"товар {article}: пустой ответ")
                continue
            self.stats.products += 1

            seller_id, shop_name = extract_seller_from_card(data.get("widgetStates") or {})
            if not seller_id:
                continue

            record = sellers.setdefault(seller_id, {
                "shop_name": shop_name, "products": [], "delivery_node": None,
            })
            if not record["shop_name"] and shop_name:
                record["shop_name"] = shop_name
            record["products"].append(article)

            # Узел доставки запоминаем из первого товара без запроса: сам
            # запрос к виджету доставки делаем позже, и только если не хватит
            # USCC или юрлица (см. enrich_seller). На 50 страницах это минус
            # ~240 запросов к складу.
            if record["delivery_node"] is None:
                record["delivery_node"] = find_delivery_node(data.get("layout"))

            self._report(index, len(product_ids), "Определяю продавцов")
            if index % 10 == 0:
                self._log(f"Обработано товаров: {index}/{len(product_ids)}, "
                          f"продавцов {len(sellers)}")
        return sellers

    def _check_warehouse(self, node: Optional[dict], article: str) -> Optional[dict]:
        """Проверяет склад продавца через виджет доставки."""
        request = build_widget_request(node, f"/product/{article}/")
        if not request:
            return None
        response = self.client.fetch_json(request["url"], method="POST", body=request["body"])
        if not response:
            return None
        # Виджетный API отдаёт состояние в конверте {"state": {...}}
        raw = json.dumps(response, ensure_ascii=False)
        info = verify_by_warehouse(raw)
        return {"is_china": info.is_china, "region": info.region, "reason": info.reason}

    def _metrics_fresh(self, known: dict, refresh_days: int) -> bool:
        """Свежие ли метрики продавца: модалку перечитывать не нужно."""
        if refresh_days <= 0:
            return True
        raw = (known.get("metrics_at") or "").strip()
        if not raw:
            return False
        try:
            fetched = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        return fetched + timedelta(days=refresh_days) >= datetime.now(timezone.utc)

    def enrich_seller(self, seller_id: str, record: dict) -> Optional[SellerMetrics]:
        """Достаёт модалку продавца и собирает метрики с вердиктом по стране."""
        modal = self.client.seller_modal(seller_id)
        parsed = parse_seller_modal(modal or {}) if modal else {}
        self.stats.requests = self.client.requests

        shop_name = record.get("shop_name") or ""
        company = parsed.get("company_name") or ""
        inn = parsed.get("inn") or ""
        uscc = parsed.get("uscc") or ""
        province = parsed.get("province") or ""

        classification = classify_seller(
            company_name=company,
            inn=inn,
            shop_name=shop_name,
            registration_block=parsed.get("registration_block", ""),
        )

        # Уровень достоверности. USCC — абсолютный; иначе смотрим склад;
        # иначе остаются эвристики, которые в дашборд не идут.
        # Российская регистрация перебивает склад. Это принципиальный момент:
        # ООО или ИП из РФ может отгружать товар из Китая, но китайским
        # продавцом от этого не становится. Раньше склад проверялся без учёта
        # юрлица, и в подтверждённые попадали российские компании
        # (ООО "АРАВИЯ", ООО "СПЛ-ТЕХНИК РУС" и подобные).
        ru_registration = parsed.get("registration_kind") in ("ogrn", "ogrnip", "inn_ru")
        is_ru_entity = bool(classification.is_russian or ru_registration)

        # Запрос склада — самый дорогой из вторичных признаков (POST к
        # виджету доставки). Он нужен только тем, у кого нет ни USCC, ни
        # российского юрлица: остальным вердикт уже известен.
        warehouse: dict = {}
        if not (uscc or classification.uscc) and not is_ru_entity:
            node = record.get("delivery_node")
            if node and record.get("products"):
                warehouse = self._check_warehouse(node, record["products"][0]) or {}

        if uscc or classification.uscc:
            verified = VERIFY_USCC
            province = province or classification.province
        elif is_ru_entity:
            verified = VERIFY_NONE
        elif warehouse.get("is_china"):
            verified = VERIFY_WAREHOUSE
            # У продавца без USCC провинцию взять больше неоткуда, кроме региона
            # склада. Раньше она оставалась пустой, и в дашборде был прочерк.
            province = province or warehouse.get("region", "")
        elif classification.is_chinese:
            verified = VERIFY_HEURISTIC
        else:
            verified = VERIFY_NONE

        # Подтверждённого продавца не понижаем без свежих данных: прогон мог
        # пройти по кэшу товаров без карточки, и «не подтверждён» здесь — это
        # отсутствие информации, а не опровержение.
        known_level = self._known.get(seller_id, {}).get("verified_by", "")
        guarded = False
        if verified in (VERIFY_HEURISTIC, VERIFY_NONE) and known_level in (
            VERIFY_USCC, VERIFY_WAREHOUSE,
        ):
            verified = known_level
            guarded = True

        is_chinese = verified in (VERIFY_USCC, VERIFY_WAREHOUSE)

        # Причины пишем целиком: по ним видно, ЧЕМ подтверждён вердикт. Для
        # склада это особенно важно — иначе «подтверждён по складу» повисает
        # без объяснения, по какому именно региону.
        reasons = list(classification.reasons)
        if guarded:
            reasons.append(
                "прежний уровень сохранён: свежих данных о складе/реквизитах нет"
            )
        if warehouse.get("reason"):
            prefix = "склад" if verified == VERIFY_WAREHOUSE else "склад не подтвердил Китай"
            reasons.append(f"{prefix}: {warehouse['reason']}")
        if is_ru_entity and not uscc:
            reasons.append(
                "российское юрлицо — китайским продавцом не считается, "
                "даже если товар едет из Китая"
            )

        metrics = SellerMetrics.from_raw(
            seller_id=seller_id,
            orders_count=parsed.get("orders_count", ""),
            reviews_count=parsed.get("reviews_count", ""),
            working_time=parsed.get("working_time", ""),
            average_rating=parsed.get("average_rating", ""),
            shop_name=shop_name,
            company_name=company,
            is_chinese=is_chinese,
            uscc=uscc or classification.uscc,
            province=province,
            registration_kind=parsed.get("registration_kind", "unknown"),
            seller_profile_url=f"https://www.ozon.ru/seller/{seller_id}/",
            sample_product_url=(
                f"https://www.ozon.ru/product/{record['products'][0]}/" if record.get("products") else ""
            ),
            verified_by=verified,
            chinese_reasons="; ".join(reasons),
        )

        if verified == VERIFY_USCC:
            self.stats.sellers_verified_uscc += 1
        elif verified == VERIFY_WAREHOUSE:
            self.stats.sellers_verified_warehouse += 1
        else:
            self.stats.sellers_rejected += 1

        return metrics

    def run(self, section_url: str, pages: int = 10) -> HarvestStats:
        """Полный проход: раздел -> товары -> продавцы -> база."""
        section_path = self.path_from_url(section_url)
        self._log(f"Раздел: {section_path}")

        product_ids = self.collect_products(section_path, pages)
        if not product_ids:
            self._log("Товары не найдены — проверьте ссылку раздела")
            return self.stats

        # Известных продавцов загружаем до определения продавцов: кэш товаров
        # и пропуск свежих модалок опираются на содержимое базы.
        if self.store is not None:
            self._known = self.store.sellers_meta()

        sellers = self.resolve_sellers(product_ids)
        self.stats.sellers_found = len(sellers)
        self._log(f"Найдено продавцов: {len(sellers)}. Проверяю реквизиты…")

        # Пишем в базу порциями по ходу проверки, а не только в конце.
        # Прогон на 50 страниц идёт минут пятнадцать, и падение в середине
        # не должно обнулять уже проделанную работу. Заодно прогресс видно в
        # дашборде сразу, не дожидаясь финала.
        BATCH = 25
        buffer: List[SellerMetrics] = []
        seen: List[Tuple[str, str]] = []     # встреченные продавцы: (id, ссылка на товар)
        added_total = 0

        def flush(force: bool = False) -> None:
            nonlocal buffer, added_total
            if self.store is None or (not force and len(buffer) < BATCH):
                return
            if not buffer:
                return
            score_all(buffer)
            added_total += self.store.add_sellers(buffer)
            buffer = []

        for index, (seller_id, record) in enumerate(sellers.items(), 1):
            if self.cancelled():
                self.stats.cancelled = True
                self._log(f"Отменено на продавце {index} из {len(sellers)}")
                break

            known = self._known.get(seller_id)
            if known and self._metrics_fresh(known, self.refresh_days):
                # Метрики свежие — модалку не перечитываем. Это главный выигрыш
                # повторных прогонов: на 50 страницах без него 270+ запросов
                # уходили бы на уже известные карточки.
                self.stats.sellers_skipped += 1
                if record.get("products"):
                    seen.append((seller_id, f"https://www.ozon.ru/product/{record['products'][0]}/"))
                self._report(index, len(sellers), "Проверяю реквизиты")
                continue

            result = self.enrich_seller(seller_id, record)
            if result:
                buffer.append(result)
            if record.get("products"):
                seen.append((seller_id, f"https://www.ozon.ru/product/{record['products'][0]}/"))
            flush()
            self._report(index, len(sellers), "Проверяю реквизиты")
            if index % 5 == 0:
                self._log(f"Проверено продавцов: {index}/{len(sellers)}")

        flush(force=True)

        if self.store is not None and not self.cancelled():
            if seen:
                self.store.mark_seen(seen)
            # Кэш «товар → продавец» освежаем одним пакетом: и новые связки,
            # и старые (продлеваем last_seen_at).
            pairs = [
                (article, seller_id)
                for seller_id, record in sellers.items()
                for article in record.get("products", [])
            ]
            if pairs:
                fresh = self.store.touch_products(pairs)
                self._log(f"Кэш товаров: {len(pairs)} связок (новых {fresh})")

        if self.store is not None:
            parts = [f"добавлено {added_total}"]
            refreshed = len(sellers) - added_total - self.stats.sellers_skipped
            if refreshed > 0:
                parts.append(f"обновлено {refreshed}")
            if self.stats.sellers_skipped:
                parts.append(f"свежих пропущено {self.stats.sellers_skipped}")
            self._log(f"В базу: {', '.join(parts)}")

        self.stats.requests = self.client.requests
        self._log(f"Готово. {self.stats.as_text()}")
        return self.stats
