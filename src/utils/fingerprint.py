"""Модуль генерации и привязки стабильного отпечатка браузера (Anti-detect Fingerprint).

Правило безопасности (решение от 04.10.2026):
1 аккаунт = 1 стабильный отпечаток + 1 резидентный прокси.

Отпечаток генерируется ОДИН РАЗ при создании профиля аккаунта и намертво
записывается в `fingerprint.json` внутри директории профиля.
При всех последующих запусках Chrome читает этот файл — антифрод видит
всегда один и тот же реальный компьютер и не считает сессию подозрительной.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# Набор согласованных аппаратных профилей реальных компьютеров
HARDWARE_PROFILES = [
    {
        "name": "win_nvidia_rtx",
        "platform": "Win32",
        "oscpu": "Windows NT 10.0; Win64; x64",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_avail_width": 1920,
        "screen_avail_height": 1040,  # Панель задач Windows
        "device_pixel_ratio": 1.0,
        "hardware_concurrency": 12,
        "device_memory": 16,
    },
    {
        "name": "win_nvidia_gtx",
        "platform": "Win32",
        "oscpu": "Windows NT 10.0; Win64; x64",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_avail_width": 1920,
        "screen_avail_height": 1040,
        "device_pixel_ratio": 1.0,
        "hardware_concurrency": 8,
        "device_memory": 16,
    },
    {
        "name": "win_intel_iris",
        "platform": "Win32",
        "oscpu": "Windows NT 10.0; Win64; x64",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_avail_width": 1920,
        "screen_avail_height": 1040,
        "device_pixel_ratio": 1.25,
        "hardware_concurrency": 8,
        "device_memory": 16,
    },
    {
        "name": "mac_apple_m2",
        "platform": "MacIntel",
        "oscpu": "Intel Mac OS X 10_15_7",
        "ua_os": "Macintosh; Intel Mac OS X 10_15_7",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, Apple M2, OpenGL 4.1)",
        "screen_width": 1728,
        "screen_height": 1117,
        "screen_avail_width": 1728,
        "screen_avail_height": 1080,
        "device_pixel_ratio": 2.0,
        "hardware_concurrency": 8,
        "device_memory": 16,
    },
    {
        "name": "win_amd_radeon",
        "platform": "Win32",
        "oscpu": "Windows NT 10.0; Win64; x64",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_avail_width": 1920,
        "screen_avail_height": 1040,
        "device_pixel_ratio": 1.0,
        "hardware_concurrency": 12,
        "device_memory": 16,
    },
]

DEFAULT_LANGUAGES = ["ru-RU", "ru", "en-US", "en"]
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CHROME_VERSION = "130"


@dataclass
class BrowserFingerprint:
    """Стабильный отпечаток браузера, привязанный к профилю."""

    account_id: str
    user_agent: str
    platform: str
    oscpu: str
    webgl_vendor: str
    webgl_renderer: str
    screen_width: int
    screen_height: int
    screen_avail_width: int
    screen_avail_height: int
    device_pixel_ratio: float
    color_depth: int = 24
    hardware_concurrency: int = 8
    device_memory: int = 16
    languages: List[str] = field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    timezone: str = DEFAULT_TIMEZONE
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> BrowserFingerprint:
        valid_keys = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    def generate_stealth_script(self) -> str:
        """Генерирует JS для Page.addScriptToEvaluateOnNewDocument.

        Скрывает серверную среду/headless и маскирует WebGL, screen, navigator.
        """
        languages_json = json.dumps(self.languages, ensure_ascii=False)
        platform_json = json.dumps(self.platform, ensure_ascii=False)
        vendor_json = json.dumps(self.webgl_vendor, ensure_ascii=False)
        renderer_json = json.dumps(self.webgl_renderer, ensure_ascii=False)

        return f"""
        (function() {{
            'use strict';

            // 1. Скрытие автоматизации webdriver
            try {{
                delete Object.getPrototypeOf(navigator).webdriver;
            }} catch (e) {{}}
            Object.defineProperty(navigator, 'webdriver', {{
                get: () => undefined,
                enumerable: false,
                configurable: true
            }});

            // 2. Платформа, язык, ядра, память
            Object.defineProperty(navigator, 'platform', {{
                get: () => {platform_json},
                configurable: true
            }});
            Object.defineProperty(navigator, 'languages', {{
                get: () => {languages_json},
                configurable: true
            }});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{
                get: () => {self.hardware_concurrency},
                configurable: true
            }});
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => {self.device_memory},
                configurable: true
            }});

            // 3. Параметры экрана
            Object.defineProperty(window.screen, 'width', {{ get: () => {self.screen_width} }});
            Object.defineProperty(window.screen, 'height', {{ get: () => {self.screen_height} }});
            Object.defineProperty(window.screen, 'availWidth', {{ get: () => {self.screen_avail_width} }});
            Object.defineProperty(window.screen, 'availHeight', {{ get: () => {self.screen_avail_height} }});
            Object.defineProperty(window.screen, 'colorDepth', {{ get: () => {self.color_depth} }});
            Object.defineProperty(window.screen, 'pixelDepth', {{ get: () => {self.color_depth} }});
            Object.defineProperty(window, 'devicePixelRatio', {{ get: () => {self.device_pixel_ratio} }});

            // 4. Реалистичный WebGL / WebGL2 (маскировка SwiftShader/Mesa на сервере)
            const patchWebGL = (proto) => {{
                if (!proto) return;
                const origGetParameter = proto.getParameter;
                proto.getParameter = function(param) {{
                    // UNMASKED_VENDOR_WEBGL
                    if (param === 37445) return {vendor_json};
                    // UNMASKED_RENDERER_WEBGL
                    if (param === 37446) return {renderer_json};
                    // VENDOR
                    if (param === 7936) return 'WebKit';
                    // RENDERER
                    if (param === 7937) return 'WebKit WebGL';
                    return origGetParameter.apply(this, [param]);
                }};
            }};
            patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
            patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

            // 5. Плагины настольного Chrome (чтобы не было пустого массива автоматизации)
            const mockPlugins = [
                {{ name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
                {{ name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
                {{ name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }}
            ];
            try {{
                Object.defineProperty(navigator, 'plugins', {{
                    get: () => mockPlugins,
                    configurable: true
                }});
            }} catch (e) {{}}

            // 6. window.chrome объект
            window.chrome = window.chrome || {{
                app: {{ isInstalled: false }},
                runtime: {{}},
                loadTimes: function() {{ return {{}}; }},
                csi: function() {{ return {{}}; }}
            }};
        }})();
        """


def fingerprint_file_path(profile_dir: Path) -> Path:
    """Путь к файлу стабильного отпечатка внутри профиля Chrome."""
    return Path(profile_dir) / "fingerprint.json"


def generate_fingerprint_for_account(
    account_id: str,
    chrome_version: str = DEFAULT_CHROME_VERSION,
) -> BrowserFingerprint:
    """Генерирует стабильный согласованный отпечаток на основе хэша аккаунта."""
    # Детерминированный выбор профиля железа по имени аккаунта
    seed = int(hashlib.sha256(account_id.encode("utf-8")).hexdigest(), 16)
    hw = HARDWARE_PROFILES[seed % len(HARDWARE_PROFILES)]

    ua = (
        f"Mozilla/5.0 ({hw['ua_os']}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_version}.0.0.0 Safari/537.36"
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    return BrowserFingerprint(
        account_id=account_id,
        user_agent=ua,
        platform=hw["platform"],
        oscpu=hw["oscpu"],
        webgl_vendor=hw["webgl_vendor"],
        webgl_renderer=hw["webgl_renderer"],
        screen_width=hw["screen_width"],
        screen_height=hw["screen_height"],
        screen_avail_width=hw["screen_avail_width"],
        screen_avail_height=hw["screen_avail_height"],
        device_pixel_ratio=hw["device_pixel_ratio"],
        hardware_concurrency=hw["hardware_concurrency"],
        device_memory=hw["device_memory"],
        languages=list(DEFAULT_LANGUAGES),
        timezone=DEFAULT_TIMEZONE,
        created_at=now_iso,
    )


def get_or_create_fingerprint(
    profile_dir: Path,
    account_id: str = "main",
    chrome_version: str = DEFAULT_CHROME_VERSION,
) -> BrowserFingerprint:
    """Загружает постоянный отпечаток профиля или создаёт и фиксирует его.

    Обеспечивает 100% стабильность: однажды созданный отпечаток НИКОГДА
    не меняется между перезапусками браузера.
    """
    path = fingerprint_file_path(profile_dir)
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            fp = BrowserFingerprint.from_dict(data)
            logger.debug(f"Загружен стабильный отпечаток для аккаунта {fp.account_id} из {path}")
            return fp
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Ошибка чтения {path} ({exc}) — перегенерирую отпечаток")

    # Генерируем новый и сохраняем
    fp = generate_fingerprint_for_account(account_id, chrome_version=chrome_version)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fp.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Создан и зафиксирован постоянный отпечаток для аккаунта {account_id}: {path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Не удалось сохранить отпечаток в {path}: {exc}")

    return fp
