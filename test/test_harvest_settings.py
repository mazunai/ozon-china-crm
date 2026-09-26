"""
Офлайн-самопроверка сохраняемых настроек сборов и статуса ночного сбора.

Проверяется то, что дашборд делает при старте из интерфейса: сохранение
ссылок и параметров обычного и ночного сбора, валидация до запуска,
переживание перезапуска (load дополняет недостающее дефолтами) и честный
статус «не идёт» для кнопки «Обновить» ночного сбора.

Запуск: PYTHONPATH=. python test/test_harvest_settings.py
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "dashboard"))

# Модуль дашборда: импортируется целиком, но сервер при импорте не стартует
_spec = importlib.util.spec_from_file_location("dashserver", REPO / "dashboard" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

PASSED = 0
TOTAL = 0


def check(condition, description):
    global PASSED, TOTAL
    TOTAL += 1
    mark = "OK " if condition else "FAIL"
    PASSED += 1 if condition else 0
    print(f"  [{mark}] {description}")
    return condition


def test_defaults_and_roundtrip(tmp: Path):
    print("Настройки: дефолты, сохранение, чтение после «перезапуска»")
    server.HARVEST_SETTINGS_FILE = tmp / "settings.json"

    settings = server.load_harvest_settings()
    check(settings["harvest"]["pages"] == 50 and settings["night"]["until"] == "08:00",
          "без файла отдаются дефолты")

    saved = server.save_harvest_settings({
        "harvest": {"url": "https://www.ozon.ru/category/noutbuki-15692/", "pages": 30, "delay": 1.0},
        "night": {"url": "https://www.ozon.ru/highlight/krupnaya-bytovaya-tehnika-15694/",
                  "chunk_pages": 5, "until": "06:30", "pause": 300, "refresh_days": 3},
    })
    check(saved["harvest"]["url"].endswith("noutbuki-15692/") and saved["night"]["until"] == "06:30",
          "сохраняются обе секции")

    # «Перезапуск»: загрузка в свежем вызове отдаёт то же самое
    again = server.load_harvest_settings()
    check(again == saved, "после сохранения load отдаёт то же — переживает перезапуск")
    check((tmp / "settings.json").exists(), "файл настроек реально лежит в data/")


def test_validation(tmp: Path):
    print("Валидация до запуска: кривые значения не сохраняются")
    server.HARVEST_SETTINGS_FILE = tmp / "settings2.json"
    server.save_harvest_settings({"night": {"url": "https://www.ozon.ru/highlight/x/"}})

    for bad, label in (
        ({"night": {"until": "завтра"}}, "«работать до» не ЧЧ:ММ"),
        ({"harvest": {"url": "https://wildberries.ru/x"}}, "чужой домен в ссылке обычного сбора"),
        ({"night": {"url": "https://example.com/"}}, "чужой домен в ссылке ночного сбора"),
        ({"harvest": {"pages": "много"}}, "страницы не число"),
    ):
        try:
            server.save_harvest_settings(bad)
            check(False, f"{label} — должно было упасть с ValueError")
        except ValueError:
            check(True, f"{label} — отклонено с ValueError")

    # Ограничения диапазонов: экстремумы срезаются, а не отклоняются
    saved = server.save_harvest_settings({
        "harvest": {"pages": 9999},
        "night": {"chunk_pages": -5, "pause": 99999, "refresh_days": 1000},
    })
    check(saved["harvest"]["pages"] == 500, "страницы обычного сбора срезаются до 500")
    check(saved["night"]["chunk_pages"] == 1 and saved["night"]["pause"] == 3600
          and saved["night"]["refresh_days"] == 365,
          "параметры ночного срезааются в допустимые диапазоны")

    # Частичное сохранение: правим только harvest — night не трогается
    server.save_harvest_settings({"night": {"pause": 600}})
    after = server.load_harvest_settings()
    check(after["night"]["pause"] == 600 and after["harvest"]["pages"] == 500,
          "частичное сохранение не трогает другую секцию")


def test_night_status_idle(tmp: Path):
    print("Статус ночного сбора, когда он не идёт: «Обновить» показывает суть")
    night = server.NIGHT
    # Свои файлы вместо живых: тест не должен трогать data/ репозитория
    night.STATE_FILE = tmp / "night_state.json"
    night.STOP_FILE = tmp / "night.stop"
    night.LOCK_FILE = tmp / "night.lock"
    night.LOG_DIR = tmp
    (tmp / "night_state.json").write_text(json.dumps({
        "next_page": 41,
        "updated_at": "2026-09-26T03:12:00",
        "last_result": "error",
        "failures": 2,
        "totals": {"products": 120, "sellers": 34, "uscc": 12, "errors": 1},
    }), encoding="utf-8")

    status = night.status()
    check(status["running"] is False, "без живого pid ночной сбор не считается идущим")
    check(status["next_page"] == 41 and status["last_result"] == "error",
          "статус отдаёт страницу продолжения и исход последнего чанка")
    check(status["days"]["sellers"] == 34, "итог ночи читается из стейта")


def run():
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        test_defaults_and_roundtrip(tmp)
        test_validation(tmp)
        test_night_status_idle(tmp)
    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(run())
