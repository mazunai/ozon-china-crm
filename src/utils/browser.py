"""
Выбор браузера для парсинга.

По умолчанию берём Camoufox, если он установлен: он подменяет отпечаток и
проходит антибот Ozon даже с серверного IP. Иначе — обычный Chrome через Selenium
(он работает на домашней машине).

Принудительно: переменная окружения или ключ OZON_BROWSER в config.txt
    OZON_BROWSER=camoufox | chrome
"""

import logging
import os

from .camoufox_manager import CAMOUFOX_AVAILABLE, CamoufoxManager
from .selenium_manager import SeleniumManager, get_proxy

logger = logging.getLogger(__name__)


def browser_kind() -> str:
    mode = (os.environ.get("OZON_BROWSER") or "").strip().lower()
    if not mode:
        try:
            from .config_loader import read_config
            mode = (read_config().get("OZON_BROWSER") or "").strip().lower()
        except Exception:
            mode = ""
    if mode in ("camoufox", "firefox"):
        return "camoufox"
    if mode in ("chrome", "selenium"):
        return "chrome"
    return "camoufox" if CAMOUFOX_AVAILABLE else "chrome"


def create_browser_manager(headless: bool = True):
    """Возвращает менеджер браузера с единым интерфейсом."""
    kind = browser_kind()
    if kind == "camoufox":
        logger.info("Браузер: Camoufox (обход антибота Ozon)")
        return CamoufoxManager(headless=headless, proxy=get_proxy())
    logger.info("Браузер: Chrome через Selenium")
    return SeleniumManager(headless=headless)
