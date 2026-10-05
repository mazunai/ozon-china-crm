#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Утилита для поиска китайских контактов (телефонов/WeChat) по USCC.
Использует публичную выдачу Baidu для извлечения мобильных номеров.
"""

import re
import requests
from bs4 import BeautifulSoup

# Регулярка для китайских мобильных номеров (11 цифр, начинаются на 13-19)
# Именно они в 99% случаев являются WeChat ID
MOBILE_REGEX = re.compile(r'\b(1[3-9]\d{9})\b')

def search_contacts_in_baidu(uscc: str) -> list:
    """Ищет USCC в Baidu и возвращает список найденных мобильных номеров."""
    print(f"[*] Ищем контакты для USCC: {uscc} ...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,ru;q=0.7'
    }
    
    # Поисковый запрос: USCC + "телефон" (电话)
    query = f"{uscc} 电话"
    url = f"https://www.baidu.com/s?wd={requests.utils.quote(query)}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text(separator=' ')
        
        # Ищем мобильные номера
        phones = MOBILE_REGEX.findall(page_text)
        
        # Возвращаем уникальные
        return list(set(phones))

    except Exception as e:
        print(f"[!] Ошибка при поиске {uscc}: {e}")
        return []

if __name__ == "__main__":
    # Тестовый пример из AGENTS.md
    test_uscc = "91330782MAEATRWE13" 
    found_phones = search_contacts_in_baidu(test_uscc)
    
    print("\n=== РЕЗУЛЬТАТЫ ===")
    if found_phones:
        print(f"Найдено возможных WeChat/Телефонов: {len(found_phones)}")
        for phone in found_phones:
            print(f" -> {phone} (Попробуйте добавить в WeChat)")
    else:
        print("Телефоны не найдены. Возможен ручной поиск в агрегаторах (Aiqicha/Qichacha).")
