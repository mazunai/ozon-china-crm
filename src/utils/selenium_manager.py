
import logging
import os
import shutil
import subprocess
import time
import json
import urllib.request
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium_stealth import stealth
from typing import Optional

logger = logging.getLogger(__name__)

# Порт отладки и профиль «обычного» Chrome. Профиль постоянный: в нём Ozon
# сохраняет cookie антибот-аттестации (abt_att), и следующие запуски проходят сразу.
DEBUG_PORT = int(os.environ.get("OZON_DEBUG_PORT", "9222"))
PROFILE_DIR = Path(os.environ.get("OZON_PROFILE", str(Path.home() / ".ozon_parser_chrome")))
# OZON_NO_ATTACH=1 — отключить режим «обычного Chrome» и вернуться к классическому webdriver
ATTACH_MODE = os.environ.get("OZON_NO_ATTACH") != "1"


def get_proxy() -> str:
    """Прокси для Chrome: OZON_PROXY (env) или ключ OZON_PROXY в config.txt.

    Формат: host:port (без авторизации, IP-whitelist) — самый надёжный вариант,
    т.к. Chrome не поддерживает логин/пароль в --proxy-server.
    """
    proxy = os.environ.get("OZON_PROXY", "").strip()
    if proxy:
        return proxy
    try:
        from .config_loader import read_config
        return (read_config().get("OZON_PROXY") or "").strip()
    except Exception:
        return ""


def resolve_proxy_for_chrome(raw_proxy: str = "") -> str:
    """Прокси для Chrome. Если в строке есть логин/пароль — поднимаем локальный
    мост (Chrome не умеет авторизацию в --proxy-server) и отдаём адрес моста."""
    raw = (raw_proxy or "").strip() or get_proxy()
    if not raw:
        return ""
    # SOCKS Chrome умеет сам (и DNS через него): HTTP-мост здесь ломал бы
    # прокси — мост говорит с апстримом на CONNECT, а тот ждёт рукопожатие
    # SOCKS. Мост нужен только для HTTP-прокси с логином/паролем.
    if raw.lower().startswith("socks5"):
        logger.info(f"Прокси для Chrome: {raw} (напрямую, SOCKS)")
        return raw
    try:
        from .proxy_bridge import start_bridge
        resolved = start_bridge(raw)
        logger.info(f"Прокси для Chrome: {resolved}")
        return resolved
    except Exception as exc:                          # noqa: BLE001
        logger.error(f"Прокси не применён ({exc}) — идём напрямую")
        return ""


def find_chrome_binary() -> Optional[str]:
    """Путь к бинарю Chrome в системе."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def debug_port_alive(port: int = DEBUG_PORT) -> bool:
    """Отвечает ли уже запущенный Chrome на порту отладки."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False


class SeleniumManager:

    def __init__(self, headless=True, profile_dir=None, debug_port=None, proxy=None, account_id=None):
        self.headless = headless
        # Профиль и порт отладки задаются на инстанс: у каждого аккаунта
        # отправителя свой Chrome — свой профиль (свои cookie сессии Ozon) и
        # свой порт. None — общий браузер парсера из переменных окружения.
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else PROFILE_DIR
        self.debug_port = int(debug_port) if debug_port else DEBUG_PORT
        self.proxy = (proxy or "").strip()
        self.account_id = str(account_id or "main").strip()
        
        # Стабильный отпечаток: загружается из fingerprint.json или генерируется 1 раз
        from .fingerprint import get_or_create_fingerprint
        self.fingerprint = get_or_create_fingerprint(self.profile_dir, account_id=self.account_id)

        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self._chrome_proc: Optional[subprocess.Popen] = None
        self._attached = False
        # Своя вкладка этого воркера. Нужна из-за режима подключения к общему
        # Chrome: все воркеры ходят через один порт отладки, и без отдельной
        # вкладки они перезаписывают друг другу страницу.
        self._tab_handle: Optional[str] = None
    
    def create_driver(self) -> webdriver.Chrome:
        """Создаёт драйвер для парсинга.

        Сначала пробуем режим «обычного Chrome»: Chrome запускается БЕЗ флагов
        автоматизации, а Selenium подключается к нему через порт отладки. Ozon
        отдаёт антибот-челлендж автоматизированному браузеру, обычный — проходит
        (в URL появляется abt_att=1). Если не получилось — классический webdriver.
        """
        if ATTACH_MODE:
            try:
                return self._attach_to_normal_chrome()
            except Exception as exc:                       # noqa: BLE001
                logger.warning(f"Режим обычного Chrome не удался ({exc}), классический запуск")

        return self._create_driver_legacy()

    @staticmethod
    def _apply_stealth(driver, fp=None) -> None:
        """Дополнительная маскировка через CDP.

        Скрывает серверную среду под Linux Xvfb/VM, подменяет WebGL (Mesa/SwiftShader)
        на согласованный GPU, маскирует screen, navigator.webdriver, плагины и таймзону.
        """
        if fp is None:
            from .fingerprint import generate_fingerprint_for_account
            fp = generate_fingerprint_for_account("main")

        try:
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": fp.user_agent,
                "acceptLanguage": ",".join(fp.languages),
                "platform": fp.platform,
            })
        except Exception as exc:                       # noqa: BLE001
            logger.debug(f"UA-подмена не удалась: {exc}")

        for command, params in (
            ("Emulation.setTimezoneOverride", {"timezoneId": fp.timezone}),
            ("Emulation.setLocaleOverride", {"locale": fp.languages[0] if fp.languages else "ru-RU"}),
        ):
            try:
                driver.execute_cdp_cmd(command, params)
            except Exception as exc:                   # noqa: BLE001
                logger.debug(f"{command} не удался: {exc}")

        script = fp.generate_stealth_script()
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
            try:
                driver.execute_script(script)
            except Exception:
                pass
        except Exception as exc:                       # noqa: BLE001
            logger.debug(f"JS-маскировка не удалась: {exc}")

        logger.info(f"Применена стабильная маскировка (аккаунт: {fp.account_id}, GPU: {fp.webgl_renderer}, OS: {fp.platform})")

    def _attach_to_normal_chrome(self) -> webdriver.Chrome:
        binary = find_chrome_binary()
        if not binary:
            raise RuntimeError("Chrome не найден в системе")

        if not debug_port_alive(self.debug_port):
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                binary,
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                f"--window-size={self.fingerprint.screen_width},{self.fingerprint.screen_height}",
            ]
            proxy = resolve_proxy_for_chrome(self.proxy)
            if proxy:
                cmd.append(f"--proxy-server={proxy}")
                # Антибот сверяет IP с данными WebRTC: без этих флагов Chrome
                # «сдаёт» настоящий адрес машины и челлендж не проходится
                cmd.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
                cmd.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")

            # headless ловится антиботом: включаем только там, где нет графики
            has_display = bool(os.environ.get("DISPLAY")) or Path("/Applications").exists()
            if self.headless and not has_display:
                cmd.append("--headless=new")

            logger.info("Запуск обычного Chrome (без флагов автоматизации)")
            self._chrome_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            deadline = time.time() + 40
            while time.time() < deadline and not debug_port_alive(self.debug_port):
                time.sleep(1)
            if not debug_port_alive(self.debug_port):
                raise RuntimeError("Chrome не открыл порт отладки за 40 секунд")

        options = Options()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.debug_port}")
        driver = webdriver.Chrome(options=options)

        # Браузер не автоматизированный — stealth и флаги не нужны и только вредят
        driver.implicitly_wait(20)
        driver.set_page_load_timeout(60)
        self.driver = driver
        self.wait = WebDriverWait(driver, 20)
        self._attached = True
        self._apply_stealth(driver, self.fingerprint)
        self._ensure_usable_page()
        self._open_own_tab()
        logger.info(f"Подключено к обычному Chrome на порту {self.debug_port} "
                    f"(профиль {self.profile_dir}), вкладка {self._tab_handle}")
        return driver

    def _open_own_tab(self) -> None:
        """Открывает воркеру отдельную вкладку и запоминает её handle.

        Без этого при подключении к общему Chrome все воркеры работают в одной
        активной вкладке и портят друг другу результаты: пока один ждёт JSON
        продавца A, другой уводит ту вкладку на продавца B, и первый читает
        чужие реквизиты. Именно так китайские компании приписывались российским
        магазинам.

        Handle берём из `current_window_handle`, а НЕ из возврата `new_window`:
        при подключении через debuggerAddress метод может вернуть None, и тогда
        вкладка теряется, а привязка молча перестаёт работать.
        """
        if not self.driver:
            return
        try:
            self.driver.switch_to.new_window("tab")
            self._tab_handle = self.driver.current_window_handle
            if not self._tab_handle:
                raise RuntimeError("current_window_handle пуст после new_window")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(
                f"Не удалось открыть отдельную вкладку ({exc}). "
                "Воркеры будут делить одну вкладку — при параллельном парсинге "
                "это даёт перепутанные данные, поэтому работаем в один поток."
            )
            self._tab_handle = None

    def has_own_tab(self) -> bool:
        """Есть ли у воркера своя вкладка. Нужно для решения о параллельности."""
        return bool(self._tab_handle) and self._attached

    def _ensure_usable_page(self) -> bool:
        """Убеждается, что есть открытая вкладка и в ней хоть что-то загружено.

        Chrome, оставленный предыдущим запуском, может не иметь ни одной
        страницы: тогда подключение к нему падает с «unable to discover open
        pages». Проверяем и при необходимости открываем вкладку сами.
        """
        if not self.driver:
            return False
        try:
            if not self.driver.window_handles:
                self.driver.switch_to.new_window("tab")
                return True
            return True
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"Не удалось обеспечить открытую вкладку: {exc}")
            return False

    def _focus_tab(self) -> None:
        """Делает активной вкладку этого воркера перед любой операцией."""
        if not self.driver or not self._tab_handle:
            return
        try:
            if self.driver.current_window_handle != self._tab_handle:
                self.driver.switch_to.window(self._tab_handle)
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"Не удалось переключиться на свою вкладку: {exc}")

    def _create_driver_legacy(self) -> webdriver.Chrome:
        chrome_options = Options()
        
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        
        if self.headless:
            chrome_options.add_argument("--headless")
        
        chrome_options.add_argument(f"--window-size={self.fingerprint.screen_width},{self.fingerprint.screen_height}")

        # Ozon блокирует IP датацентров: без резидентского прокси будет антибот-челлендж
        proxy = resolve_proxy_for_chrome(self.proxy)
        if proxy:
            chrome_options.add_argument(f"--proxy-server={proxy}")
        
        try:
            driver = webdriver.Chrome(options=chrome_options)
            

            stealth(driver,
                   languages=self.fingerprint.languages,
                   vendor="Google Inc.",
                   platform=self.fingerprint.platform,
                   webgl_vendor=self.fingerprint.webgl_vendor,
                   renderer=self.fingerprint.webgl_renderer,
                   fix_hairline=True)
            self._apply_stealth(driver, self.fingerprint)
            

            driver.implicitly_wait(20)
            driver.set_page_load_timeout(60)
            
            self.driver = driver
            self.wait = WebDriverWait(driver, 20)
            
            logger.info("Chrome драйвер создан успешно")
            return driver
            
        except WebDriverException as e:
            logger.error(f"Ошибка создания Chrome драйвера: {e}")
            raise
    
    def create_driver_with_logging(self) -> webdriver.Chrome:
        chrome_options = Options()
        
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option('perfLoggingPrefs', {'enableNetwork': True, 'enablePage': False})
        chrome_options.add_experimental_option('loggingPrefs', {'performance': 'ALL'})
        
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        
        if self.headless:
            chrome_options.add_argument("--headless")
        
        chrome_options.add_argument("--window-size=1920,1080")

        # Ozon блокирует IP датацентров: без резидентского прокси будет антибот-челлендж
        proxy = resolve_proxy_for_chrome()
        if proxy:
            chrome_options.add_argument(f"--proxy-server={proxy}")
        
        try:
            driver = webdriver.Chrome(options=chrome_options)
            
            stealth(driver,
                   languages=["ru-RU", "ru"],
                   vendor="Google Inc.",
                   platform="Win32",
                   webgl_vendor="Intel Inc.",
                   renderer="Intel Iris OpenGL Engine",
                   fix_hairline=True)
            
            driver.implicitly_wait(20)
            driver.set_page_load_timeout(60)
            
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            self.driver = driver
            self.wait = WebDriverWait(driver, 20)
            
            logger.info("Chrome драйвер с логированием создан успешно")
            return driver
            
        except WebDriverException as e:
            logger.error(f"Ошибка создания Chrome драйвера с логированием: {e}")
            raise
    
    def navigate_to_url(self, url: str) -> bool:
        if not self.driver:
            logger.error("Драйвер не инициализирован")
            return False
        
        try:
            # Работаем только в своей вкладке: в общем Chrome активной может
            # быть вкладка другого воркера
            self._focus_tab()
            logger.debug(f"Переход по URL: {url}")
            self.driver.get(url)
            

            self._wait_for_antibot_bypass()
            
            return True
            
        except TimeoutException:
            logger.error(f"Таймаут при загрузке: {url}")
            return False
        except WebDriverException as e:
            logger.error(f"Ошибка WebDriver: {e}")
            return False
    
    def wait_for_json_response(self, timeout: int = 90) -> Optional[str]:
        if not self.driver:
            return None
        
        try:
            self._focus_tab()
            logger.debug("Ожидание JSON ответа...")
            start_time = time.time()
            

            WebDriverWait(self.driver, 30).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            

            while time.time() - start_time < timeout:
                try:
                    page_source = self.driver.page_source
                    json_content = self._extract_json_from_html(page_source)
                    
                    if json_content:
                        try:
                            data = json.loads(json_content)
                            if 'widgetStates' in data:
                                logger.debug("JSON ответ с widgetStates найден")
                                return json_content
                        except json.JSONDecodeError:
                            pass
                    
                    time.sleep(2.5)  # Увеличенное время ожидания между проверками
                    
                except Exception as e:
                    logger.debug(f"Ошибка проверки содержимого страницы: {e}")
                    time.sleep(2.5)  # Увеличенное время ожидания при ошибке
                    continue
            
            logger.warning(f"Таймаут ожидания JSON ответа после {timeout} секунд")
            return self._extract_json_from_html(self.driver.page_source)
            
        except Exception as e:
            logger.error(f"Ошибка ожидания JSON ответа: {e}")
            return None
    
    def _extract_json_from_html(self, html_content: str) -> Optional[str]:
        try:
            import re
            

            pre_pattern = r'<pre[^>]*>(.*?)</pre>'
            pre_match = re.search(pre_pattern, html_content, re.DOTALL | re.IGNORECASE)
            
            if pre_match:
                json_content = pre_match.group(1).strip()
                logger.debug("JSON найден в <pre> теге")
                return json_content
            

            first_brace = html_content.find('{')
            last_brace = html_content.rfind('}')
            
            if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
                json_content = html_content[first_brace:last_brace + 1]
                logger.debug("JSON найден по поиску скобок")
                return json_content
            
            return None
            
        except Exception as e:
            logger.error(f"Ошибка извлечения JSON из HTML: {e}")
            return None
    
    def _wait_for_antibot_bypass(self, max_wait_time: int = 240):   # ⬅️ 120 → 240
        start_time = time.time()
        reload_attempts = 0
        max_reload_attempts = 3

        while time.time() - start_time < max_wait_time:
            try:
                if self._is_blocked():
                    if reload_attempts < max_reload_attempts:
                        logger.info(
                            f"Обнаружена блокировка, перезагрузка страницы "
                            f"(попытка {reload_attempts + 1}/{max_reload_attempts})"
                        )
                        self.driver.refresh()
                        reload_attempts += 1
                        time.sleep(15)      
                        continue
                    else:
                        logger.warning("Превышено кол-во попыток, возвращаем новый драйвер")
                        raise Exception("Access blocked after retries")
                else:
                    logger.info("Антибот защита пройдена")
                    return
            except Exception as e:
                if "Access blocked" in str(e):
                    raise
                time.sleep(15)  
                continue

        logger.warning(f"Антибот защита не пройдена за {max_wait_time} секунд")
        raise Exception("Antibot timeout")
    
    def _is_blocked(self) -> bool:
        if not self.driver:
            return True
            
        try:
            blocked_indicators = [
                "cloudflare", "checking your browser", "enable javascript",
                "access denied", "blocked", "ddos-guard", "проверка браузера",
                "доступ ограничен", "access restricted"
            ]
            
            page_source = self.driver.page_source.lower()
            
            for indicator in blocked_indicators:
                if indicator in page_source:
                    return True
                    
            return False
            
        except Exception:
            return True
    
    def close(self):
        if self.driver:
            try:
                if self._attached:
                    # Браузер общий для всех воркеров — закрываем только свою
                    # вкладку, сам Chrome оставляем живым (в нём cookie
                    # аттестации Ozon).
                    try:
                        if self._tab_handle and self._tab_handle in self.driver.window_handles:
                            self.driver.switch_to.window(self._tab_handle)
                            self.driver.close()
                    except Exception:
                        pass
                else:
                    self.driver.quit()
                logger.debug("Драйвер закрыт успешно")
            except Exception as e:
                logger.error(f"Ошибка закрытия драйвера: {e}")
            finally:
                self.driver = None
                self.wait = None
                self._tab_handle = None

    def kill_browser(self):
        """Полностью закрыть запущенный нами Chrome (освобождает память)."""
        if self._chrome_proc and self._chrome_proc.poll() is None:
            try:
                self._chrome_proc.terminate()
            except Exception:
                pass
        self._chrome_proc = None