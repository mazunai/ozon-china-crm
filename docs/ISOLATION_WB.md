# Изоляция WB-парсера от Ozon-CRM (конвенция для обеих сессий)

Оба парсера живут в одном репозитории и на одной ВМ. Правила, чтобы данные
не пересекались и парсеры работали параллельно.

## Данные: раздельные базы

- **Ozon-CRM**: `data/sellers.sqlite3` — продавцы Ozon, статусы рассылки,
  история переписки. Читается дашбордом (`dashboard/server.py`).
- **WB**: `data/wb_sellers.sqlite3` — продавцы Wildberries, своё хранилище
  (аналог SellerStore). **НЕ передавать** `WBSellerHarvester`'у Ozon-ный
  `SellerStore`: тогда WB-продавцы попадут в очередь рассылки Ozon.
- Общие утилиты (`outreach.scoring`, `parsers.chinese_filter`) — можно,
  это код, а не данные.

## Сеть: раздельные транспорты

- Ozon: Chrome с профилем (`~/.ozon_parser_chrome`, порт отладки 9222),
  темп `OzonPageClient` (adaptive), замок `BROWSER_LOCK` в дашборде.
- WB: `curl_cffi` с impersonate=chrome (не Selenium), темп `wb_pace`.
  Настоящий браузер не трогает — поэтому параллельная работа возможна.
- У каждого свой lock/heartbeat: `data/night_harvest.lock` — только Ozon.

## Логи

- Ozon: `logs/ozon_parser_*.log`, ночной сбор: `logs/night_harvest_*.log`.
- WB: `logs/wb_*.log` — свой префикс.

## Безопасность (общая)

- Секреты только в `.env` (chmod 600), в git не попадают.
- Дашборд: пароль + замок перебора (6 неудач с IP → блок 10 минут),
  Secure-cookie по HTTPS. Наружу только 443 (Caddy); порт 8000 закрыт
  в группе безопасности — доступ к дашборду только через домен.
- ВМ 2 ГБ: два парсера одновременно — следить за памятью (Chrome + curl_cffi).
