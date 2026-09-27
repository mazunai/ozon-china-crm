"""
Офлайн-самопроверка verify.py: второй уровень подтверждения страны — склад
из виджета доставки. Аудит: единственный модуль подтверждения без тестов;
его поломка молча переводит продавцов из warehouse-подтверждённых в отсев.

Образцы строк — из AGENTS.md §3.2 и живых проверок (Shandong Sheng и др.).

Запуск: PYTHONPATH=. python test/test_verify.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crm.verify import parse_warehouse_sections  # noqa: E402

PASSED = 0
TOTAL = 0


def check(condition, description):
    global PASSED, TOTAL
    TOTAL += 1
    mark = "OK " if condition else "FAIL"
    PASSED += 1 if condition else 0
    print(f"  [{mark}] {description}")
    return condition


def state_with(description_text: str) -> dict:
    """Состояние виджета доставки с одной строкой descriptionRs."""
    return {"sections": [{"type": "addressSelect", "descriptionRs": [
        {"content": description_text},
    ]}]}


def test_warehouse_parsing():
    print("Подтверждение по складу: китайский регион, РФ, прочая заграница")
    cn = parse_warehouse_sections(state_with("Со склада продавца, Shandong Sheng"))
    check(cn.is_china and cn.region == "Shandong Sheng",
          "Shandong Sheng — склад в Китае")

    sh = parse_warehouse_sections(state_with("Со склада продавца, Shanghai"))
    check(sh.is_china, "Shanghai (город без суффикса) — Китай")

    ru = parse_warehouse_sections(state_with("Со склада продавца, Ленинградская область"))
    check(not ru.is_china and "России" in ru.reason,
          "кириллический регион — склад в России, не Китай")

    tr = parse_warehouse_sections(state_with("Со склада продавца, Istanbul"))
    check(not tr.is_china and "не Китай" in tr.reason,
          "Istanbul — заграница, но не Китай (ловушка из AGENTS.md)")

    # Секция global_info: доставка из-за рубежа даже без строки склада
    cross = {"sections": [
        {"type": "global_info", "descriptionRs": [{"content": "Условия доставки"}]},
        {"type": "addressSelect", "descriptionRs": [{"content": "Со склада продавца, Shandong Sheng"}]},
    ]}
    info = parse_warehouse_sections(cross)
    check(info.is_china and info.is_cross_border,
          "global_info фиксирует кросс-бордер, склад по-прежнему разбирается")

    # Пустое/битое состояние — безопасный дефолт
    empty = parse_warehouse_sections({})
    check(not empty.is_china and not empty.is_cross_border,
          "пустой виджет — ни Китай, ни кросс-бордер (отказ без фантазий)")


def run():
    test_warehouse_parsing()
    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(run())
