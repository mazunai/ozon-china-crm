"""
Драйвер на Camoufox — Firefox со спуффингом отпечатка.

Зачем: Ozon отдаёт антибот-челлендж обычному Chrome на сервере (нет GPU →
программный WebGL, мало шрифтов, серверные признаки), причём даже когда трафик
идёт с «чистого» домашнего IP. Camoufox подменяет отпечаток (WebGL, шрифты,
navigator, таймзона) и проходит проверку — проверено на VM в Yandex Cloud
с прямого датацентрового IP.

Класс повторяет интерфейс SeleniumManager, чтобы парсеры работали без правок:
    create_driver() / navigate_to_url() / wait_for_json_response() / close()
Плюс у драйвера тот же мини-API, что используют парсеры:
    get() / page_source / execute_script() / find_element() / refresh() / quit()
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

try:
    from camoufox.sync_api import Camoufox
    CAMOUFOX_AVAILABLE = True
except Exception:                                     # noqa: BLE001
    Camoufox = None                                   # type: ignore
    CAMOUFOX_AVAILABLE = False


def _css(by, value: str) -> str:
    """Переводит Selenium-локатор (By.ID и др.) в CSS-селектор Playwright."""
    by = str(by).lower()
    if by in ("css selector", "css"):
        return value
    if by == "id":
        return f"#{value}"
    if by == "class name":
        return f".{value}"
    if by == "tag name":
        return value
    if by == "xpath":
        return f"xpath={value}"
    return value


class _CamoufoxElement:
    """Обёртка над Playwright-локатором с Selenium-подобным API."""

    def __init__(self, page, locator, index: int = 0):
        self._page = page
        self._locator = locator
        self._index = index

    def find_elements(self, by, value: str) -> List["_CamoufoxElement"]:
        inner = self._locator.nth(self._index).locator(_css(by, value))
        count = inner.count()
        return [_CamoufoxElement(self._page, inner, i) for i in range(count)]

    def find_element(self, by, value: str) -> "_CamoufoxElement":
        found = self.find_elements(by, value)
        if not found:
            raise LookupError(f"элемент не найден: {value}")
        return found[0]

    def get_attribute(self, name: str) -> Optional[str]:
        try:
            value = self._locator.nth(self._index).get_attribute(name)
        except Exception:
            return None
        # Playwright отдаёт href/src как в разметке (относительными), а парсер
        # ожидает абсолютный URL — как их возвращает Selenium
        if value and name in ("href", "src") and value.startswith("/"):
            try:
                return urljoin(self._page.url, value)
            except Exception:
                return value
        return value


class _CamoufoxDriver:
    """Мини-драйвер: методы, которые вызывают парсеры."""

    def __init__(self, page, context):
        self.page = page
        self._context = context
        self.implicitly_wait = lambda *_: None          # noqa: E731
        self.set_page_load_timeout = lambda *_: None    # noqa: E731

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)

    @property
    def page_source(self) -> str:
        try:
            return self.page.content()
        except Exception:
            return ""

    @property
    def title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    @property
    def current_url(self) -> str:
        return self.page.url

    def execute_script(self, script: str, *args):
        return self.page.evaluate(script)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=90000)

    def find_elements(self, by, value: str) -> List[_CamoufoxElement]:
        locator = self.page.locator(_css(by, value))
        try:
            count = locator.count()
        except Exception:
            return []
        return [_CamoufoxElement(self.page, locator, i) for i in range(count)]

    def find_element(self, by, value: str) -> _CamoufoxElement:
        found = self.find_elements(by, value)
        if not found:
            raise LookupError(f"элемент не найден: {value}")
        return found[0]

    def quit(self) -> None:
        try:
            self._context.close()
        except Exception:
            pass


class CamoufoxManager:
    """Тот же интерфейс, что у SeleniumManager, но на Camoufox."""

    BLOCK_MARKERS = ("abt-challenge", "проверка браузера", "checking your browser")

    def __init__(self, headless: bool = True, proxy: str = ""):
        self.headless = headless
        self.proxy = proxy
        self.driver: Optional[_CamoufoxDriver] = None
        self._camoufox = None

    def create_driver(self) -> _CamoufoxDriver:
        if not CAMOUFOX_AVAILABLE:
            raise RuntimeError("camoufox не установлен: pip install 'camoufox[geoip]' && python -m camoufox fetch")

        options = {"headless": self.headless, "humanize": True}
        if self.proxy:
            options["proxy"] = {"server": self.proxy if "://" in self.proxy else f"http://{self.proxy}"}
            options["geoip"] = True

        logger.info(f"Запуск Camoufox (headless={self.headless}, proxy={self.proxy or 'нет'})")
        self._camoufox = Camoufox(**options)
        browser = self._camoufox.__enter__()
        context = browser.new_context()
        page = context.new_page()
        self.driver = _CamoufoxDriver(page, context)
        logger.info("Camoufox готов к работе")
        return self.driver

    # --- интерфейс SeleniumManager --------------------------------------- #
    def navigate_to_url(self, url: str) -> bool:
        if not self.driver:
            logger.error("Драйвер не инициализирован")
            return False
        try:
            self.driver.get(url)
            return True
        except Exception as exc:                       # noqa: BLE001
            logger.error(f"Ошибка загрузки {url[:80]}: {exc}")
            return False

    def wait_for_json_response(self, timeout: int = 90) -> Optional[str]:
        """Ждёт JSON-ответ Ozon (страницы API отдают чистый JSON в body)."""
        if not self.driver:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                text = self.driver.page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception:
                text = ""
            text = text.strip()
            if text.startswith("{"):
                try:
                    if "widgetStates" in json.loads(text):
                        return text
                except json.JSONDecodeError:
                    pass
            time.sleep(2)
        logger.warning(f"JSON не получен за {timeout}с")
        return None

    def is_blocked(self) -> bool:
        if not self.driver:
            return True
        source = self.driver.page_source.lower()
        return any(marker in source for marker in self.BLOCK_MARKERS)

    def close(self) -> None:
        if self._camoufox is not None:
            try:
                self._camoufox.__exit__(None, None, None)
            except Exception:
                pass
            finally:
                self._camoufox = None
                self.driver = None
        logger.debug("Camoufox закрыт")
