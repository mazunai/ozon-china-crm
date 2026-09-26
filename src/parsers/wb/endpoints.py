"""Адреса и версии внутренних API Wildberries.

Версии меняются часто: карточка за год переехала v2 → v4 (v2 отдаёт 404,
проверено 26.09.2026). Поэтому каждый эндпоинт — СПИСОК кандидатов: клиент
запоминает первую рабочую версию и дальше не перебирает. Новый путь API
добавляется одной строкой в список, без правки логики.
"""

# dest — гео-идентификатор региона, от него зависят цены и остатки.
# -1257786 = Москва. Переопределяется переменной WB_DEST.
DEFAULT_DEST = -1257786

# Поиск/выдача категории. query= и page= обязательны.
SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/{version}/search"
SEARCH_VERSIONS = ("v18", "v13", "v5")

# Карточки товаров, батч до ~100 артикулов через nm=1;2;3.
CARD_URL = "https://card.wb.ru/cards/{version}/detail"
CARD_VERSIONS = ("v4",)

# Реквизиты продавца (ИНН/ОГРН, адрес, юрлицо). Единственный источник страны.
# Статус на 26.09.2026: путь v2 отдаёт 404 — актуальный нужно зафиксировать
# с чистого IP (DevTools на странице продавца). Пока перебираем известных
# кандидатов; если ни один не отвечает — продавец пропускается с ошибкой.
SELLER_INFO_URLS = (
    "https://static-basket-01.wbbasket.ru/vol0/data/seller-info-by-id-api/v2/{supplier_id}",
    "https://static-basket-01.wbbasket.ru/vol0/data/seller-info-by-id/{supplier_id}",
)

# Каталог всех товаров продавца — задел под «собрать витрину продавца целиком».
SELLER_CATALOG_URL = "https://catalog.wb.ru/sellers/{version}/catalog"
SELLER_CATALOG_VERSIONS = ("v2", "v1")
