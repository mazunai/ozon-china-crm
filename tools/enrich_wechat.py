#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# Регулярка для китайских мобильных номеров (11 цифр, начинаются на 13-19)
MOBILE_REGEX = re.compile(r'\b(1[3-9]\d{9})\b')

def search_contacts_in_baidu(uscc: str) -> list:
    print(f"[*] Ищем контакты для USCC: {uscc} ...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,ru;q=0.7'
    }
    
    # Ищем название и контакты
    query = f"{uscc} 联系方式" 
    url = f"https://www.baidu.com/s?wd={requests.utils.quote(query)}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text(separator=' ')
        
        # Ищем мобильные номера
        phones = MOBILE_REGEX.findall(page_text)
        return list(set(phones))
    except Exception as e:
        print(f"[!] Ошибка при поиске {uscc}: {e}")
        return []

def main():
    db_path = Path(__file__).parent.parent / 'data' / 'sellers.sqlite3'
    if not db_path.exists():
        print(f"БД не найдена: {db_path}")
        return
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Берем 3 уникальных USCC
    cursor.execute("""
        SELECT DISTINCT uscc, company_name 
        FROM sellers 
        WHERE uscc != '' AND is_chinese = 1 
        LIMIT 3
    """)
    rows = cursor.fetchall()
    
    print(f"Взято {len(rows)} уникальных USCC из базы.")
    
    for uscc, company in rows:
        print(f"\n======================================")
        print(f"Компания: {company} | USCC: {uscc}")
        phones = search_contacts_in_baidu(uscc)
        
        if phones:
            print(f"✅ Найдено возможных WeChat/Телефонов: {len(phones)}")
            for phone in phones:
                print(f" -> {phone}")
        else:
            print("❌ Телефоны в публичной выдаче Baidu не найдены. Нужен ручной парсинг Qichacha/Aiqicha.")

if __name__ == "__main__":
    main()
