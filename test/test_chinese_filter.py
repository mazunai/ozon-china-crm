"""
Офлайн-самопроверка классификатора китайских продавцов.
Не требует сети/Selenium — только чистые данные.

Запуск: python test/test_chinese_filter.py

ВАЖНО про контракт (изменён 25.09.2026)
---------------------------------------
Раньше сильные сигналы (USCC, иероглифы, «Китай», телефон +86, плашка CN)
искались по всему сырому JSON карточки. На практике это давало ложные вердикты
«китайский продавец» у российских ИП: в JSON десятки служебных токенов, а слово
«Китай» встречается в описаниях товаров и отзывах покупателей. На реальных
данных из output/ так были помечены 137 товаров, среди которых оказался
российский продавец с валидным ИНН.

Теперь сильные сигналы берутся ТОЛЬКО из полей продавца:
    registration_block — блок реквизитов из модалки (строки через <br>)
    company_name, shop_name, inn
Сырой JSON (raw_json) влияет лишь на слабые признаки и ничего не доказывает.

Поэтому часть кейсов ниже намеренно INVERTED_ — они проверяют, что сигнал,
поданный через raw_json, китайским продавца не делает. Это не ослабление
тестов, а фиксация исправленного поведения.
"""
import importlib.util
import sys
from pathlib import Path

# Загружаем модуль напрямую по пути: src/parsers/__init__.py импортирует Selenium,
# который для этой офлайн-проверки не нужен.
_MOD_PATH = Path(__file__).parent.parent / "src" / "parsers" / "chinese_filter.py"
_spec = importlib.util.spec_from_file_location("chinese_filter", _MOD_PATH)
chinese_filter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chinese_filter)

classify_seller = chinese_filter.classify_seller
extract_seller_registration = chinese_filter.extract_seller_registration
province_from_uscc = chinese_filter.province_from_uscc

# Набор кейсов: (описание, аргументы, ожидаемое is_chinese)
CASES = [
    # --- Настоящие китайские продавцы: USCC в блоке реквизитов ---------------
    (
        "КИТАЙ: USCC в реквизитах продавца",
        dict(
            company_name="Shenzhen Xingye Trading Co., Ltd",
            inn="91440300MA5G1P2X3K",
            registration_block=(
                "Shenzhen Xingye Trading Co., Ltd<br>Shenzhen<br>91440300MA5G1P2X3K"
            ),
        ),
        True,
    ),
    (
        "КИТАЙ: реальная модалка senyi2dian (Иу, Чжэцзян)",
        dict(
            company_name="Yiwushisenyibaozhuangzhipinyouxiangongsi",
            inn="91330782MAEATRWE13",
            shop_name="senyi2dian",
            registration_block=(
                "Yiwushisenyibaozhuangzhipinyouxiangongsi<br>yiwushi<br>91330782MAEATRWE13"
            ),
        ),
        True,
    ),
    (
        "КИТАЙ: реальная модалка Rhapsody Home Shop (Наньчан, Цзянси)",
        dict(
            company_name="Nanchang Yangluo Network Technology Co.",
            inn="91360111MAC3F9Y20A",
            shop_name="Rhapsody Home Shop",
            registration_block=(
                "Nanchang Yangluo Network Technology Co.<br>"
                "401, Unit 1, Block 8, Yiran Yuan, Luojia Township<br>91360111MAC3F9Y20A"
            ),
        ),
        True,
    ),
    (
        "КИТАЙ: USCC передан только в поле ИНН (парсер нашёл его в реквизитах)",
        dict(company_name="Hangzhou Yuntang e-commerce Co., LTD", inn="91330106MA2B1XYZ9K"),
        True,
    ),
    (
        "КИТАЙ: Longyan ... Fujian, USCC 91350802MAEF646B8L в реквизитах",
        dict(
            company_name="Longyan Silla Huanchao Youpin Technology Co., Ltd.",
            inn="91350802MAEF646B8L",
            registration_block=(
                "Longyan Silla Huanchao Youpin Technology Co., Ltd.<br>"
                "Room 2721, Wanda Center, Building B1, No. 1 Shuanglong Road, "
                "Xinluo District, Longyan City, Fujian P<br>91350802MAEF646B8L"
            ),
        ),
        True,
    ),

    # --- Российские продавцы: не должны попадать в китайские -----------------
    (
        "РОССИЯ: ООО + валидный RU ИНН 10 цифр",
        dict(company_name='ООО «РОБОТКОМП КОРП»', inn="7707083893", raw_json=""),
        False,
    ),
    (
        "РОССИЯ: ИП + валидный RU ИНН 12 цифр",
        dict(company_name="ИП Иванов И.И.", inn="771234567890", raw_json=""),
        False,
    ),
    (
        "РОССИЯ: ИП Дубовик, ОГРНИП 15 цифр, в товаре «Китай»",
        dict(
            company_name="ИП Дубовик Василий Николаевич",
            inn="319703100086469",
            shop_name="Homemag",
            raw_json='{"characteristics":{"Страна-изготовитель":"Китай"}}',
            registration_block="ИП Дубовик Василий Николаевич<br>319703100086469",
        ),
        False,
    ),
    (
        "РОССИЯ: ИП Дубовик без упоминаний Китая",
        dict(company_name="ИП Дубовик Василий Николаевич", inn="319703100086469", raw_json=""),
        False,
    ),
    (
        "РОССИЯ: ИП Маликов — 15-значный ОГРНИП не должен читаться как «китайский»",
        dict(
            company_name="ИП Маликов Сергей Петрович",
            inn="304500012345678",
            registration_block="ИП Маликов Сергей Петрович<br>304500012345678",
        ),
        False,
    ),
    (
        "РОССИЯ: русский магазин без признаков Китая",
        dict(
            company_name='ООО «Ромашка»',
            inn="7712345678",
            shop_name="Ромашка",
            raw_json='{"description":"Доставка по России, работаем с 2015 года"}',
        ),
        False,
    ),
    (
        "РОССИЯ: русский ИП с адресом в РФ",
        dict(
            company_name="ИП Смирнов А.В.",
            inn="771234567890",
            raw_json='{"address":"г. Москва, ул. Ленина, д. 5, кв. 12"}',
        ),
        False,
    ),

    # --- Мусор из интерфейса: реальные ложные плюсы прошлой сессии ----------
    (
        "МУСОР: в поле компании попал текст интерфейса «Фото и видео покупателей»",
        dict(company_name="Фото и видео покупателей", inn="", shop_name="ooo clothing1"),
        False,
    ),
    (
        "МУСОР: в поле компании попало «Товар закончился»",
        dict(company_name="Товар закончился", inn="", shop_name="3D Печатный Арт 3"),
        False,
    ),
    (
        "МУСОР: пустые данные о продавце",
        dict(company_name="", inn=""),
        False,
    ),
    (
        "МУСОР: латиница без USCC и без российского номера — не доказательство",
        dict(company_name="Bold Craft", inn="", shop_name="monkey-trading"),
        False,
    ),

    # --- INVERTED_: сигналы через сырой JSON больше не делают продавца китайским
    (
        "INVERTED: USCC лежит только в сыром JSON — сам по себе не доказательство",
        # Название намеренно без китайского города и пиньиня: проверяем именно
        # то, что USCC из служебного JSON не делает продавца китайским. Если бы
        # в названии был Shenzhen, вывод «Китай» был бы обоснован, и кейс
        # перестал бы что-либо проверять.
        dict(
            company_name="Nordwind Handels GmbH",
            inn="",
            raw_json='{"seller":{"inn":"91440300MA5G1P2X3K","name":"Shenzhen Xingye Trading Co., Ltd"}}',
        ),
        False,
    ),
    (
        "INVERTED: «Китай» в описании товара — не доказательство",
        dict(company_name="Магазин Ромашка", inn="", raw_json='{"origin":"Китай"}'),
        False,
    ),
    (
        "INVERTED: иероглифы в описании магазина (raw_json) — не доказательство",
        dict(company_name="Best Deals", inn="", raw_json='{"description":"我们只做正品"}'),
        False,
    ),
    (
        "INVERTED: китайский телефон в сыром JSON — не доказательство",
        dict(company_name="Sunrise Store", inn="", raw_json='{"phone":"+86 13812345678"}'),
        False,
    ),
    (
        "INVERTED: плашка CN в сыром JSON — не доказательство",
        dict(company_name="Sunrise Home Store", inn="", raw_json='{"country":"CN","delivery":"global"}'),
        False,
    ),
    (
        "INVERTED: «Почта России» — способ доставки, не признак страны",
        dict(company_name="Lucky Shop", inn="", raw_json='{"origin":"CN — Китай","delivery":"Почта России"}'),
        False,
    ),

    # --- Другие страны: глобалка, но не КНР ---------------------------------
    (
        "ТУРЦИЯ: пометка другой страны -> не Китай",
        dict(
            company_name="Istanbul Tekstil Limited Shtirkati",
            inn="",
            raw_json='{"origin":"Турция","delivery":"Доставка из-за рубежа"}',
        ),
        False,
    ),

    # --- Слабые сигналы, которые продолжают работать ------------------------
    (
        "КИТАЙ (слабо): пиньинь в названии магазина",
        dict(company_name="", inn="", shop_name="Guoji Maoyi Youpin Store", raw_json=""),
        True,
    ),
]


def run():
    passed = 0
    for desc, kwargs, expected in CASES:
        res = classify_seller(**kwargs)
        ok = res.is_chinese == expected
        passed += ok
        flag = "OK " if ok else "FAIL"
        print(f"[{flag}] {desc}")
        print(f"      is_chinese={res.is_chinese} (ожидалось {expected})  conf={res.confidence:.2f}  other={res.other_country}")
        print(f"      reasons: {res.reasons}")
        print()

    # Отдельно: разбор реквизитов и провинция из кода региона
    print("--- провинция из USCC (цифры 3-8 = код региона GB/T 2260) ---")
    province_cases = [
        ("91330782MAEATRWE13", "Zhejiang"),
        ("91320594MA1YTH5N49", "Jiangsu"),
        ("91360106MA7FP0LG42", "Jiangxi"),
        ("91350802MA8RLMCX72", "Fujian"),
        ("91350104MA33Y3XK25", "Fujian"),
        ("", ""),
    ]
    province_ok = 0
    for uscc, expected in province_cases:
        got = province_from_uscc(uscc)
        good = got == expected
        province_ok += good
        print(f"  [{'OK ' if good else 'FAIL'}] {uscc or '(пусто)'} -> {got or '(нет)'} (ожидалось {expected or '(нет)'})")

    print("\n--- блок реквизитов: китайский против российского ---")
    reg_cases = [
        ("91330782MAEATRWE13", "uscc_cn", "Zhejiang"),
        ("319703100086469", "ogrnip", ""),
        ("7707083893", "inn_ru", ""),
    ]
    reg_ok = 0
    for reg, kind, province in reg_cases:
        parsed = extract_seller_registration(f"Компания<br>Адрес<br>{reg}" if kind == "uscc_cn" else f"Компания<br>{reg}")
        good = parsed["kind"] == kind and parsed["province"] == province
        reg_ok += good
        print(f"  [{'OK ' if good else 'FAIL'}] {reg:<20} kind={parsed['kind']:<8} провинция={parsed['province'] or '(нет)'}")

    total = len(CASES) + len(province_cases) + len(reg_cases)
    passed += province_ok + reg_ok
    print(f"\nРезультат: {passed}/{total} кейсов прошло")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
