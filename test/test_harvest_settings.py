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


def test_parse_live():
    print("Живая фаза ночного сбора по хвосту лога (строки — из боевого лога 26.09)")
    parse = server.NIGHT._parse_live

    # Фаза сбора товаров: свежайшая строка — «Страница N:»
    live = parse([
        "чанк: страницы 321–420",
        "Страница 321: товаров на странице 8, новых 8",
        "Страница 321: товаров на странице 8, новых 8",
        "Страница 322: товаров на странице 8, новых 8",
    ])
    check(live["phase"] == "собираю товары" and live["page"] == 322,
          "фаза «собираю товары», страница — из самой свежей строки")
    check(live["chunk_start"] == 321 and live["chunk_end"] == 420,
          "диапазон прохода найден по строке «чанк: страницы»")

    # Фаза определения продавцов: «Страница» уже далеко позади
    live = parse([
        "чанк: страницы 421–520",
        "Страница 435: товаров на странице 8, новых 0",
        "Обработано товаров: 10/91, продавцов 10",
        "Обработано товаров: 20/91, продавцов 20",
        "Обработано товаров: 30/91, продавцов 29",
    ])
    check(live["phase"] == "определяю продавцов" and live["items_done"] == 30
          and live["items_total"] == 91 and live["sellers_found"] == 29,
          "фаза «определяю продавцов» с числами из свежей строки")

    # Фаза проверки реквизитов
    live = parse([
        "Обработано товаров: 90/91, продавцов 82",
        "Найдено продавцов: 83. Проверяю реквизиты…",
        "Проверено продавцов: 5/83",
        "Проверено продавцов: 40/83",
    ])
    check(live["phase"] == "проверяю реквизиты" and live["checked_done"] == 40
          and live["checked_total"] == 83,
          "фаза «проверяю реквизиты» с прогрессом")

    # Проход доигран: свежайшая строка — ночная сводка, дальше будет пауза
    live = parse([
        "Проверено продавцов: 80/83",
        "В базу: добавлено 8, свежих пропущено 75",
        "Готово. страниц=15, товаров=17, продавцов=83",
        "[26.09 17:38:40]   итог: страниц 15, товаров 17, продавцов 83",
        "[26.09 17:38:40] за ночь: чанков 2, товаров 91, продавцов 366 (USCC 37), следующая страница 521",
    ])
    check(live["phase"] == "пауза между проходами", "после итога чанка — пауза между проходами")

    # Пустой лог — пустая фаза, интерфейс покажет «ожидание запуска»
    live = parse([])
    check(live["phase"] == "" and live["page"] == 0, "пустой лог — фаза не определена")


def test_resume_after_restart(tmp: Path):
    print("Ночной сбор переживает перезапуск дашборда")
    import os
    from datetime import datetime, timedelta

    night = server.NIGHT
    night.STATE_FILE = tmp / "night_state.json"
    night.STOP_FILE = tmp / "night.stop"
    night.LOCK_FILE = tmp / "night.lock"
    night.LOG_DIR = tmp
    server.HARVEST_SETTINGS_FILE = tmp / "settings.json"
    server.save_harvest_settings({"night": {
        "url": "https://www.ozon.ru/category/noutbuki-15692/",
        "chunk_pages": 7, "until": "06:00", "pause": 90, "refresh_days": 5,
    }})

    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)

    real_start = night.start
    night.start = fake_start
    night_log = tmp / datetime.now().strftime("night_harvest_%Y-%m-%d.log")

    def write_state(fresh=True):
        moment = datetime.now() if fresh else datetime.now() - timedelta(hours=2)
        (tmp / "night_state.json").write_text(json.dumps({
            "next_page": 321,
            "updated_at": moment.isoformat(timespec="seconds"),
        }), encoding="utf-8")

    try:
        # 1) убит SIGKILL'ом (lock остался, финиша нет) → продолжаем
        write_state(fresh=True)
        (tmp / "night.lock").write_text("999999", encoding="utf-8")
        night_log.write_text(
            "[26.09 18:00:01] чанк: страницы 321–420\n"
            "Страница 322: товаров на странице 8, новых 8\n",
            encoding="utf-8")
        message = server.resume_night_after_restart()
        check(bool(message) and started.get("chunk_pages") == 7
              and started.get("until") == "06:00" and started.get("refresh_days") == 5,
              "убитый без финиша ночной сбор продолжен с настройками")

        # 2) убит SIGTERM'ом (финиш есть, но стоп-крана и «время вышло» нет) →
        #    тоже продолжаем: рестарт дашборда не должен отменять ночь
        started.clear()
        write_state(fresh=True)
        night_log.write_text(
            "[26.09 18:10:13] остановка по сигналу\n"
            "[26.09 18:10:13] финиш: чанков 2, товаров 0, продавцов 0\n",
            encoding="utf-8")
        (tmp / "night.lock").unlink(missing_ok=True)
        message = server.resume_night_after_restart()
        check(bool(message) and started.get("chunk_pages") == 7,
              "SIGTERM-убийство (финиш без причины остановки) тоже возобновляется")

        # 3) живой pid в lock (реальный запуск) → не мешаем
        started.clear()
        (tmp / "night.lock").write_text(str(os.getpid()), encoding="utf-8")
        check(server.resume_night_after_restart() is None and not started,
              "живой ночной сбор не трогается")

        # 4) стоп-кран → возобновления нет
        started.clear()
        (tmp / "night.lock").unlink(missing_ok=True)
        (tmp / "night.stop").write_text("стоп\n", encoding="utf-8")
        check(server.resume_night_after_restart() is None and not started,
              "стоп-кран уважается: сбор не возобновляется")
        (tmp / "night.stop").unlink()

        # 5) сбор закончился сам («время вышло») → не возобновляем
        started.clear()
        night_log.write_text(
            "[26.09 06:00:02] время вышло — завершаю\n"
            "[26.09 06:00:02] финиш: чанков 9, товаров 540, продавцов 1200\n",
            encoding="utf-8")
        check(server.resume_night_after_restart() is None and not started,
              "штатное завершение по времени не возобновляется")

        # 6) прогресс устарел (>3 часов) → не возобновляем
        started.clear()
        write_state(fresh=False)
        check(server.resume_night_after_restart() is None and not started,
              "устаревший прогресс не возобновляется")
    finally:
        night.start = real_start


def run():
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        test_defaults_and_roundtrip(tmp)
        test_validation(tmp)
        test_night_status_idle(tmp)
        test_parse_live()
        test_resume_after_restart(tmp)
    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(run())
