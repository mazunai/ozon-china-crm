"""
Классификатор «китайский продавец / товар из Китая».

Важно: внутренний API Ozon неофициальный и меняется без предупреждений, поэтому
вместо привязки к одному конкретному имени поля детектор работает по НАБОРУ
признаков (сигналов) и возвращает уверенность 0..1 + список сработавших причин.
Это устойчиво к переименованию полей: если Ozon поменяет ключ — сработают
другие сигналы.

Признаки группируются по надёжности:

  Сильные (почти гарантируют Китай):
    - 18-значный Unified Social Credit Code (кит. налоговый код, цифры+латиница)
    - явная строка-маркер «Китай / China / 中国» в JSON карточки

  Средние (показывают, что продавец иностранный, но не обязательно КНР):
    - нет валидного российского ИНН (RU ИНН — строго 10 или 12 цифр)
    - название компании на латинице (у Global-продавцов Ozon требует латиницу)

  Уточняющие (подтверждают именно КНР среди прочих «глобалок»):
    - название города в пиньини (Shenzhen, Guangzhou, Yiwu, ...)
    - оргформа зарубежного юрлица (Co., Ltd / Trading / Technology / ...)
    - маркеры «из-за рубежа / из Китая» в данных о доставке

Примечание про «русских» китайцев: продавец, зарегистрированный как ООО/ИП
в РФ, имеет валидный RU ИНН и выглядит как российский — его детектор по
этим полям НЕ назовёт китайским (это и правильно по данным Ozon). Такие
«китайские по происхождению» продавцы определяются только по внешним
источникам (адрес регистрации, аналитика), см. комментарии в README.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple


# --- Константы для настройки --------------------------------------------------

# Порог уверенности, начиная с которого продавец считается китайским
CHINESE_THRESHOLD = 0.45

# 18-значный китайский Unified Social Credit Code: 91/92/93 + 6 цифр кода
# региона + 10 символов. Только эта форма считается доказательством КНР.
#
# Раньше здесь был ещё «общий» шаблон \b[0-9A-Z]{18}\b с хотя бы одной буквой.
# Он давал ложные срабатывания: под него попадали любые 18-символьные служебные
# токены Ozon (трекинг-ключи, хвосты ссылок на картинки), которых в JSON
# карточки десятки. Из-за этого российским ИП с валидным ИНН приписывался
# «китайский код» и защита от российского юрлица отключалась.
USCC_CN_RE = re.compile(r"\b9[123]\d{6}[0-9A-Z]{10}\b")

# Российские регистрационные номера: ОГРН 13 цифр, ОГРНИП 15 цифр.
RU_OGRN_RE = re.compile(r"\b\d{15}\b|\b\d{13}\b")

# Провинции Китая в пиньине — в адресе магазина это прямой признак КНР
CHINA_PROVINCE_TOKENS = [
    "fujian", "zhejiang", "guangdong", "jiangsu", "shandong", "henan",
    "hebei", "sichuan", "hubei", "hunan", "anhui", "jiangxi", "shaanxi",
    "yunnan", "guangxi", "liaoning", "jilin", "heilongjiang", "shanxi",
    "guizhou", "gansu", "hainan", "xinjiang", "ningxia", "qinghai",
]

# Слова из китайских адресов в латинице (сами по себе слабые — считаем в сумме)
ADDRESS_TOKENS = [
    "room ", "building ", "district", "road", "street", "industrial park",
    "economic development zone", "no.", "floor", "block", "village",
]

# Позиции, которые мы ИЩЕМ как подтверждение именно КНР (пиньинь)
CHINA_CITY_TOKENS = [
    "shenzhen", "guangzhou", "yiwu", "hangzhou", "foshan", "dongguan",
    "ningbo", "zhuhai", "quanzhou", "xiamen", "shanghai", "beijing",
    "suzhou", "wuxi", "changzhou", "taizhou", "cixi", "wenzhou",
    "nanning", "guilin", "dali", "hefei", "jinhua", "tonglu",
    "longyan", "shantou", "zhongshan", "jiangmen", "huizhou", "putian",
    "jinjiang", "shishi", "nanchang", "wuhan", "chengdu", "chongqing",
    "xi'an", "xian", "zhengzhou", "jinan", "qingdao", "yantai", "weihai",
    "shenyang", "dalian", "harbin", "changchun", "kunming", "guiyang",
    "lanzhou", "urumqi", "haikou", "sanya", "luoyang", "weifang",
    "linyi", "shaoxing", "jiaxing", "huzhou", "yangzhou", "nantong",
    "xuzhou", "linhai", "ruili", "baoding", "lianyungang", "dezhou",
    "fuzhou", "changsha", "kunshan", "zhangzhou", "chaozhou", "shunde",
    "nanhai", "yueqing", "ruian", "yongkang", "haining", "xiaoshan",
    "tangshan", "langfang", "cangzhou", "zibo", "jining", "taian",
    "xinxiang", "nanyang", "xiangyang", "yichang", "zhuzhou", "hengyang",
    "liuzhou", "zhanjiang", "maoming", "jieyang", "shanwei", "meizhou",
    "anshan", "fushun", "jilin", "qinhuangdao", "handan", "xingtai",
    "hengshui", "liaocheng", "heze", "zaozhuang", "rizhao", "suqian",
    "huaian", "yancheng", "taizhou", "zhenjiang", "huzhou", "quzhou",
    "lishui", "jingdezhen", "pingxiang", "xinyu", "yingtan",
]

# Пиньинь-слова, характерные для названий китайских компаний и магазинов
CHINA_PINYIN_TOKENS = [
    "youpin", "maoyi", "keji", "gongsi", "shangmao", "jinchu", "chukou",
    "wangluo", "shuma", "zhineng", "guoji", "jituan", "baihuo", "shangwu",
    "gongying", "dianzi", "fuzhuang", "riyong", "jiaju", "tongcheng",
    "xinxi", "kejimaoyi", "shanghang", "huoyuan", "dianpu",
]

# Китайский телефон: +86 1XXXXXXXXXX (мобильные КНР начинаются с 13-19)
CHINA_PHONE_RE = re.compile(r"(?:\+?86[\s\-]?)1[3-9]\d{9}")

# Плашка страны отправки «CN» (Китай) в карточке/магазине
CN_BADGE_RE = re.compile(
    r'"country(?:code)?"\s*:\s*"?CN\b'
    r'|"(?:badge|flag|iso|origin)"\s*:\s*"CN"'
    r'|\bCN\s*[-—(]\s*(?:Китай|China)'
    r'|(?:Китай|China)\s*[-—(]?\s*CN\b'
    r'|"delivery(?:type|mode|scheme)"\s*:\s*"?(?:global|crossborder|cross_border)',
    re.IGNORECASE,
)

# Признаки международной доставки (Ozon Global везёт неделями, часто почтой)
GLOBAL_DELIVERY_TOKENS = [
    "ozon global", "из-за рубежа", "из за рубежа", "международн",
    "почта россии", "отправка из китая", "доставка из китая",
    "cross-border", "crossborder", " internationale",
]

# Российские организационно-правовые формы и регистрационные номера.
# ОГРН — 13 цифр, ОГРНИП — 15 цифр; в паре с кириллицей это однозначно РФ.
RU_LEGAL_FORMS = ("ооо", "ип", "зао", "оао", "пао", "нко", "ао")

# Иероглифы — прямое указание на китайский текст в описании магазина
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Оргформы, характерные для зарубежных (в т.ч. кит.) юрлиц
LATIN_LEGAL_TOKENS = [
    "co., ltd", "co.,ltd", "co ltd", "co.,", "limited", "ltd",
    "trading", "technology", "electronics", "industrial", "appliance",
    "group", "import", "export", "trade",
]

# Явные строки-маркеры «Китай» (ищем в lower-cased тексте JSON)
CHINA_TEXT_MARKERS = ["китай", "china", "chinese", "中国", "из китая", "из кит"]

# Маркеры «из-за рубежа / Global» (подтверждают cross-border, но не страну)
GLOBAL_TEXT_MARKERS = [
    "из-за рубежа", "из рубежа", "из зарубеж", "global", "crossborder",
    "cross_border", "cross border", "доставка из",
]

# Маркеры ДРУГИХ стран — отменяют «китай» (глобалка, но не КНР)
OTHER_COUNTRY_MARKERS = {
    "турция": ["турция", "turkey", "türkiye", "türkiye"],
    "казахстан": ["казахстан", "kazakhstan"],
    "беларусь": ["беларусь", "belarus", "белару"],
    "корея": ["корея", "south korea", "korea"],
    "таиланд": ["таиланд", "thailand"],
}


@dataclass
class SellerClassification:
    is_chinese: bool
    confidence: float                 # 0..1
    reasons: list = field(default_factory=list)   # человекочитаемые причины
    other_country: Optional[str] = None           # если глобалка, но не КНР
    is_russian: bool = False                      # явно российский продавец
    uscc: str = ""                                # китайский код из реквизитов
    province: str = ""                            # провинция Китая (из USCC)
    registration_kind: str = "unknown"            # uscc_cn / ogrn / ogrnip / inn_ru

    def to_dict(self):
        return {
            "is_chinese": self.is_chinese,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "other_country": self.other_country,
            "is_russian": self.is_russian,
            "uscc": self.uscc,
            "province": self.province,
            "registration_kind": self.registration_kind,
        }


def ru_inn_is_valid(inn: str) -> bool:
    """Публичная проверка: российский ИНН — строго 10 или 12 цифр."""
    return _ru_inn_is_valid(inn)


def find_uscc(text: str) -> Optional[str]:
    """Публичная обёртка: найти китайский USCC строго по форме кода."""
    return _find_uscc(text)


def _ru_inn_is_valid(inn: str) -> bool:
    """RU ИНН — строго 10 или 12 цифр."""
    return bool(inn) and inn.isdigit() and len(inn) in (10, 12)


def _name_is_latin(name: str) -> bool:
    if not name:
        return False
    has_latin = bool(re.search(r"[A-Za-z]", name))
    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", name))
    return has_latin and not has_cyrillic


def _norm_text(raw_json: str) -> str:
    """Расшифровываем \\uXXXX-экранировки кириллицы, чтобы маркеры находились
    и в «сыром», и в декодированном виде. Возвращаем текст в исходном регистре."""
    if not raw_json:
        return ""
    decoded = raw_json
    try:
        decoded = json.dumps(json.loads(raw_json), ensure_ascii=False)
    except Exception:
        decoded = raw_json
    return raw_json + " " + decoded


def _find_uscc(text: str) -> Optional[str]:
    """Ищет китайский USCC строго по форме кода (91/92/93 + регион + 10 знаков).

    Никаких «похожих на код» строк: раньше сюда попадали служебные токены Ozon,
    и это давало ложные вердикты «Китай» у российских продавцов.
    """
    if not text:
        return None
    match = USCC_CN_RE.search(text)
    return match.group(0) if match else None


def extract_seller_registration(text: str) -> dict:
    """Разбирает блок реквизитов продавца из модалки.

    В модалке Ozon это `textBlock` с `textColor: tertiary`, где строки склеены
    через `<br>`. Проверено вживую:

        китайский продавец — три строки:
            Yiwushisenyibaozhuangzhipinyouxiangongsi
            yiwushi
            91330782MAEATRWE13          <- USCC
        российский — две строки:
            ИП Дубовик Василий Николаевич
            319703100086469             <- ОГРНИП

    Возвращает разобранные поля; пустые значения — если блок не опознан.
    """
    result = {
        "company": "", "address": "", "registration": "",
        "kind": "unknown", "uscc": "", "province": "",
    }
    if not text:
        return result

    parts = [p.strip() for p in str(text).split("<br>") if p.strip()]
    if not parts:
        return result

    result["company"] = parts[0]
    if len(parts) >= 3:
        result["address"] = parts[1]
        result["registration"] = parts[-1]
    elif len(parts) == 2:
        result["registration"] = parts[-1]

    uscc = _find_uscc(result["registration"])
    if uscc:
        result["kind"] = "uscc_cn"
        result["uscc"] = uscc
        result["province"] = province_from_uscc(uscc)
        return result

    if RU_OGRN_RE.fullmatch(result["registration"] or ""):
        length = len(result["registration"])
        result["kind"] = "ogrnip" if length == 15 else "ogrn"
        return result

    if (result["registration"] or "").isdigit() and len(result["registration"]) in (10, 12):
        result["kind"] = "inn_ru"
        return result

    return result


# Провинции Китая по первым двум цифрам кода региона GB/T 2260 (цифры 3-8 USCC).
USCC_PROVINCES = {
    "11": "Beijing", "12": "Tianjin", "13": "Hebei", "14": "Shanxi",
    "15": "Inner Mongolia", "21": "Liaoning", "22": "Jilin", "23": "Heilongjiang",
    "31": "Shanghai", "32": "Jiangsu", "33": "Zhejiang", "34": "Anhui",
    "35": "Fujian", "36": "Jiangxi", "37": "Shandong", "41": "Henan",
    "42": "Hubei", "43": "Hunan", "44": "Guangdong", "45": "Guangxi",
    "46": "Hainan", "50": "Chongqing", "51": "Sichuan", "52": "Guizhou",
    "53": "Yunnan", "54": "Tibet", "61": "Shaanxi", "62": "Gansu",
    "63": "Qinghai", "64": "Ningxia", "65": "Xinjiang", "71": "Taiwan",
    "81": "Hong Kong", "82": "Macau",
}


def province_from_uscc(uscc: str) -> str:
    """Провинция Китая из USCC: цифры 3-8 — код региона, первые две — провинция.

    Проверено на живых продавцах: 330782 -> Zhejiang (Иу), 320594 -> Jiangsu
    (Сучжоу), 360111/360106 -> Jiangxi (Наньчан), 350802 -> Fujian (Лунъянь) —
    совпало с адресом в строке выше.
    """
    if not uscc or len(uscc) < 8:
        return ""
    return USCC_PROVINCES.get(uscc[2:4], "")


def classify_seller(
    company_name: str = "",
    inn: str = "",
    raw_json: str = "",
    shop_name: str = "",
    threshold: float = CHINESE_THRESHOLD,
    registration_block: str = "",
) -> SellerClassification:
    """
    Классифицирует продавца/товар по данным, которые уже собирает парсер.

    Аргументы:
      company_name       — юрлицо (SellerInfo.company_name / ProductInfo.company_name)
      inn                — ИНН или регистрационный номер. Может быть пустым или
                           содержать китайский USCC или российский ОГРН/ОГРНИП.
      raw_json           — сырой JSON карточки. ВАЖНО: используется ТОЛЬКО для
                           слабых сигналов (маркеры cross-border). Сильные сигналы
                           из него больше не берутся — см. пояснение ниже.
      shop_name          — название магазина на витрине.
      registration_block — блок реквизитов продавца из модалки (строки через
                           `<br>`). Главный источник USCC.

    Почему сильные сигналы ограничены полями продавца
    -------------------------------------------------
    Раньше USCC и маркеры «Китай» искались по всему сырому JSON, и это давало
    ложные вердикты: в JSON карточки десятки служебных токенов, а слова «Китай»
    встречаются в описаниях товаров и отзывах. В итоге российский ИП с валидным
    ИНН получал «китайский код» и помечался китайским продавцом. Теперь USCC
    берётся только из реквизитов продавца, а маркеры — из его названий.
    """
    company = (company_name or "").strip()
    shop = (shop_name or "").strip()
    name = (company + " " + shop).strip()          # и юрлицо, и витрина

    # Только поля, принадлежащие продавцу. Сюда НЕ попадает raw_json.
    seller_text = " ".join(x for x in (company, shop, registration_block or "", inn or "") if x)
    seller_lower = seller_text.lower()
    # Слабые сигналы можно смотреть шире — по сырому JSON, но они ничего не
    # доказывают и не перебивают российское юрлицо.
    wide_text = _norm_text(raw_json).lower()

    confidence = 0.0
    reasons = []
    other_country = None

    # ---- Реквизиты продавца: самый надёжный источник ----
    registration = extract_seller_registration(registration_block) if registration_block else {}
    uscc = registration.get("uscc") or _find_uscc(inn or "")
    province = registration.get("province", "")
    reg_kind = registration.get("kind", "unknown")
    # Если блок реквизитов не передали, но ИНН похож на российский номер —
    # это тоже подтверждение российской регистрации.
    if reg_kind == "unknown" and (inn or "").strip():
        stripped_inn = (inn or "").strip()
        if stripped_inn.isdigit():
            if len(stripped_inn) == 15:
                reg_kind = "ogrnip"
            elif len(stripped_inn) == 13:
                reg_kind = "ogrn"
            elif len(stripped_inn) in (10, 12):
                reg_kind = "inn_ru"

    if uscc:
        confidence += 0.6
        reasons.append(f"USCC в реквизитах продавца: {uscc}")
        if province:
            reasons.append(f"провинция по коду: {province}")

    # Иероглифы — только в названии продавца, а не где-то в описании товара
    has_cjk = bool(CJK_RE.search(seller_text))
    if has_cjk:
        confidence += 0.3
        reasons.append("иероглифы в названии продавца")

    strong_china = bool(uscc) or has_cjk
    # Сигналы, специфичные ИМЕННО для Китая: без хотя бы одного из них продавец
    # может быть любой «глобалкой» (Турция, Казахстан) и китайским не считается.
    china_specific = strong_china

    # ---- Российская регистрация ----
    ru_registration = reg_kind in ("ogrn", "ogrnip", "inn_ru")
    if ru_registration:
        reasons.append(f"российская регистрация: {reg_kind}")

    # Китайский номер телефона: +86 1XXXXXXXXXX (только в данных продавца)
    phone = CHINA_PHONE_RE.search(seller_text)
    if phone:
        confidence += 0.35
        china_specific = True
        reasons.append(f"китайский телефон в данных: {phone.group(0)}")

    # ---- Плашка страны CN (Китай) ----
    if CN_BADGE_RE.search(seller_text):
        confidence += 0.4
        china_specific = True
        reasons.append("найдена плашка страны CN (Китай)")

    # ---- Явное «Китай/China» в названии продавца ----
    if seller_lower and any(mk in seller_lower for mk in CHINA_TEXT_MARKERS):
        confidence += 0.4
        china_specific = True
        reasons.append("в названии продавца указано «Китай/China»")

    # ---- Международная доставка (Ozon Global) ----
    for token in GLOBAL_DELIVERY_TOKENS:
        if token in wide_text or token in seller_lower:
            confidence += 0.1
            reasons.append(f"международная доставка: {token}")
            break

    # ---- Маркеры других стран ----
    # В JSON карточки почти всегда есть служебные тексты про доставку в другие
    # страны, поэтому «другая страна» только фиксируется и НЕ отменяет вердикт,
    # если уже есть сильные признаки Китая.
    for country, marks in OTHER_COUNTRY_MARKERS.items():
        if wide_text and any(mk in wide_text for mk in marks):
            other_country = country
            break

    # ---- Российское юрлицо (считаем до «нет ИНН», чтобы не путать ОГРНИП с ИНН) ----
    name_lower_ru = name.lower()
    ru_form = any(f in name_lower_ru for f in RU_LEGAL_FORMS)
    cyrillic = bool(re.search(r"[А-Яа-яЁё]", name))
    ogrn_like = bool(RU_OGRN_RE.fullmatch((inn or "").strip()))
    russian_entity = (
        _ru_inn_is_valid(inn)                       # настоящий российский ИНН
        or (ogrn_like and (cyrillic or ru_form))    # ОГРН/ОГРНИП при русском названии
        or (ru_form and cyrillic)
    )

    # ---- Средние сигналы: отсутствие валидного RU ИНН ----
    if not _ru_inn_is_valid(inn) and not russian_entity:
        confidence += 0.2
        reasons.append("нет валидного российского ИНН (10/12 цифр) — иностранный продавец")

    # ---- Латинское название (Ozon Global требует латиницу) ----
    if _name_is_latin(name):
        confidence += 0.15
        reasons.append("название компании на латинице")

    # ---- Уточняющие: пиньинь-город / провинция / оргформа ----
    name_lower = name.lower()
    reg_address = (registration.get("address") or "").lower()
    # Сильные уточнения ищем только в данных продавца: его название и адрес из
    # блока реквизитов. Сырой JSON тут не используется — там лежат тексты
    # отзывов, описания товаров и данные других продавцов.
    seller_haystack = f"{name_lower} {reg_address}".strip()

    for token in CHINA_PROVINCE_TOKENS:
        if token in seller_haystack:
            confidence += 0.3
            china_specific = True
            reasons.append(f"провинция Китая в адресе продавца: {token}")
            break

    for city in CHINA_CITY_TOKENS:
        if city in seller_haystack:
            confidence += 0.25
            china_specific = True
            reasons.append(f"город в пиньини: {city}")
            break

    # Провинция, вычисленная из самого USCC (цифры 3-8 — код региона)
    if province:
        confidence += 0.2
        reasons.append(f"провинция из USCC: {province}")

    # Пиньинь-слова из нейминга китайских компаний и магазинов
    # («...Youpin Technology Co., Ltd», «...Guoji Maoyi»)
    for pinyin in CHINA_PINYIN_TOKENS:
        if pinyin in name_lower:
            confidence += 0.2
            china_specific = True
            reasons.append(f"пиньинь в названии: {pinyin}")
            break

    for legal in LATIN_LEGAL_TOKENS:
        if legal in name_lower:
            confidence += 0.1
            reasons.append(f"зарубежная оргформа: {legal}")
            break

    # Китайский адрес в латинице: Room 2721, Wanda Center, Building B1, ... Road,
    # Xinluo District — набор таких слов сам по себе не доказательство, но в
    # сумме с остальными признаками усиливает вердикт.
    if reg_address:
        addr_hits = sum(1 for tok in ADDRESS_TOKENS if tok in reg_address)
        if addr_hits >= 3:
            confidence += 0.1
            reasons.append(f"адрес в китайском формате (совпало слов: {addr_hits})")

    # ---- Маркеры «из-за рубежа» (слабый сигнал) ----
    if any(mk in wide_text for mk in GLOBAL_TEXT_MARKERS) or any(
        mk in seller_lower for mk in GLOBAL_TEXT_MARKERS
    ):
        confidence += 0.1
        reasons.append("есть признак cross-border («из-за рубежа»)")

    if russian_entity or ru_registration:
        reasons.append("признаки российского юрлица (ИП/ООО/ОГРН/ИНН)")

    # ---- Итог ----
    # Перебить подтверждённую российскую регистрацию может только USCC из
    # реквизитов: это единственный бесспорный признак китайского юрлица.
    # Иероглифы и телефон +86 сами по себе тоже указывают на Китай, но если у
    # продавца валидный ИНН/ОГРН, он российский — такие часто торгуют китайским
    # товаром, и путать их с китайскими продавцами нельзя.
    hard_china = bool(uscc)
    if (russian_entity or ru_registration) and not hard_china:
        is_chinese = False
    elif china_specific and confidence >= threshold:
        is_chinese = True
    else:
        is_chinese = False
        if other_country:
            reasons.append("признаков Китая нет")

    return SellerClassification(
        is_chinese=is_chinese,
        confidence=min(confidence, 1.0),
        reasons=reasons,
        other_country=other_country,
        is_russian=russian_entity or ru_registration,
        uscc=uscc or "",
        province=province or "",
        registration_kind=reg_kind,
    )
