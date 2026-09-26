#!/bin/bash
# Запуск дашборда Ozon-парсера локально: http://127.0.0.1:8000
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Создаю окружение и ставлю зависимости (один раз)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet selenium selenium-stealth requests beautifulsoup4 lxml openpyxl aiogram curl-cffi
fi
echo "Дашборд: http://127.0.0.1:8000  (остановить: Ctrl+C)"
exec .venv/bin/python dashboard/server.py --host 127.0.0.1 --port 8000
