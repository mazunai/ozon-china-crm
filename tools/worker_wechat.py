#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Воркер для автоматического обогащения базы WeChat-контактами через Aiqicha (Baidu).
Использует встроенный в проект CamoufoxManager для обхода базового антифрода.
"""

import sys
import time
import sqlite3
import logging
import re
from pathlib import Path
from bs4 import BeautifulSoup

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.camoufox_manager import CamoufoxManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

MOBILE_REGEX = re.compile(r'\b(1[3-9]\d{9})\b')

def get_pending_sellers(db_path: Path, limit: int = 10) -> list:
    """Получает из базы лидов, у которых еще не искали WeChat (нет метки в notes)."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT seller_id, uscc, company_name 
        FROM sellers 
        WHERE is_chinese = 1 AND uscc != '' AND notes NOT LIKE '%WeChat:%' AND notes NOT LIKE '%WeChat Not Found%'
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def append_to_notes(db_path: Path, seller_id: str, text: str):
    """Безопасно дописывает результат в поле notes."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT notes FROM sellers WHERE seller_id = ?", (seller_id,))
    row = cursor.fetchone()
    if row:
        current_notes = row[0] or ""
        new_notes = f"{current_notes} | {text}".strip(" |")
        cursor.execute("UPDATE sellers SET notes = ? WHERE seller_id = ?", (new_notes, seller_id))
        conn.commit()
    conn.close()

def extract_phones(page_source: str) -> list:
    """Вытаскивает уникальные номера телефонов из HTML."""
    soup = BeautifulSoup(page_source, 'html.parser')
    text = soup.get_text(separator=' ')
    phones = MOBILE_REGEX.findall(text)
    return list(set(phones))

def main():
    db_path = Path(__file__).parent.parent / 'data' / 'sellers.sqlite3'
    if not db_path.exists():
        logging.error(f"БД не найдена по пути: {db_path}")
        return

    sellers = get_pending_sellers(db_path, limit=20)
    if not sellers:
        logging.info("Очередь пуста: у всех китайских селлеров уже проверен WeChat.")
        return

    logging.info(f"Взято в работу {len(sellers)} селлеров.")
    
    # Запускаем браузер с подменой отпечатков (Camoufox)
    # headless=True для сервера, если нужно видеть глазами — поменяйте на False
    manager = CamoufoxManager(headless=True)
    driver = manager.create_driver()

    try:
        for seller_id, uscc, company in sellers:
            logging.info(f"Обработка: {company} (USCC: {uscc})")
            
            # Запрос напрямую в агрегатор Aiqicha
            url = f"https://aiqicha.baidu.com/s?q={uscc}"
            manager.navigate_to_url(url)
            
            # Ждем прогрузки SPA (Vue/React)
            time.sleep(5)
            
            phones = extract_phones(driver.page_source)
            
            if phones:
                phones_str = ",".join(phones)
                logging.info(f"✅ Найдено: {phones_str}")
                append_to_notes(db_path, seller_id, f"WeChat: {phones_str}")
            else:
                logging.warning("❌ Не найдено")
                append_to_notes(db_path, seller_id, "WeChat Not Found")
                
            # Задержка между запросами, чтобы не ловить капчу
            time.sleep(3)
            
    except Exception as e:
        logging.error(f"Критическая ошибка парсинга: {e}")
    finally:
        manager.close()
        logging.info("Воркер завершил работу.")

if __name__ == "__main__":
    main()
