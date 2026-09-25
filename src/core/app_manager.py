import logging
import threading
import asyncio
import time
from typing import Dict, Any, List, Optional
from ..config.settings import Settings
from ..parsers.link_parser import OzonLinkParser
from ..parsers.product_parser import OzonProductParser
from ..parsers.seller_parser import OzonSellerParser
from ..utils.excel_exporter import ExcelExporter
from ..telegram.bot_manager import TelegramBotManager
from ..utils.resource_manager import resource_manager

logger = logging.getLogger(__name__)

class AppManager:
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.is_running = False  # Глобальный флаг для совместимости
        self.active_parsing_users = set()  # Множество активных пользователей
        self.parsing_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_results = {}  # Глобальные результаты для совместимости
        self.user_results = {}  # Результаты по пользователям: {user_id: results}
        self.telegram_bot: Optional[TelegramBotManager] = None
    
    def start_parsing(self, category_url: str, selected_fields: list = None, user_id: str = None) -> bool:
        with self.parsing_lock:
            # Проверяем, не парсит ли уже этот пользователь
            if user_id and user_id in self.active_parsing_users:
                logger.warning(f"Пользователь {user_id} уже запустил парсинг")
                return False
            
            # Добавляем пользователя в активные
            if user_id:
                self.active_parsing_users.add(user_id)
            
            # Устанавливаем глобальный флаг для первого пользователя
            if not self.is_running:
                self.stop_event.clear()
                self.is_running = True
        
        try:
            # Запускаем парсинг в отдельном потоке
            parsing_thread = threading.Thread(
                target=self._parsing_task_wrapper,
                args=(category_url, selected_fields, user_id),
                daemon=True
            )
            parsing_thread.start()
            return True
        except Exception as e:
            logger.error(f"Ошибка запуска парсинга для пользователя {user_id}: {e}")
            # Убираем пользователя из активных при ошибке
            with self.parsing_lock:
                if user_id and user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                # Если это был последний пользователь, сбрасываем глобальный флаг
                if not self.active_parsing_users:
                    self.is_running = False
            return False
    
    def _parsing_task_wrapper(self, category_url: str, selected_fields: list = None, user_id: str = None):
        """Wrapper для парсинга с правильной очисткой ресурсов"""
        try:
            self._parsing_task(category_url, selected_fields, user_id)
        except Exception as e:
            logger.error(f"Ошибка в парсинге для пользователя {user_id}: {e}")
        finally:
            # Убираем пользователя из активных
            with self.parsing_lock:
                if user_id and user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                    logger.info(f"Пользователь {user_id} завершил парсинг")
                
                # Если это был последний пользователь, сбрасываем глобальный флаг
                if not self.active_parsing_users:
                    self.is_running = False
                    logger.info("Все пользователи завершили парсинг")
    
    def stop_parsing(self, user_id: str = None):
        """Останавливает парсинг для конкретного пользователя или всех"""
        with self.parsing_lock:
            if user_id:
                # Останавливаем парсинг для конкретного пользователя
                if user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                    logger.info(f"Остановлен парсинг для пользователя {user_id}")
            else:
                # Останавливаем все парсинги
                self.active_parsing_users.clear()
                logger.info("Остановлен парсинг для всех пользователей")
            
            # Если нет активных пользователей, сбрасываем глобальный флаг
            if not self.active_parsing_users:
                self.stop_event.set()
                self.is_running = False
    
    def _parsing_task(self, category_url: str, selected_fields: list = None, user_id: str = None):
        start_time = time.time()
        link_parser = OzonLinkParser(category_url, self.settings.MAX_PRODUCTS, user_id)
        
        success, product_links = link_parser.start_parsing()
        
        if self.stop_event.is_set():
            return
        
        if not success or not product_links:
            logger.error("Не удалось собрать ссылки товаров")
            return
        
        if self.stop_event.is_set():
            return
        
        product_parser = OzonProductParser(self.settings.MAX_WORKERS, user_id)
        product_results = product_parser.parse_products(product_links)
        
        # Принудительно закрываем все воркеры продуктов перед началом парсинга продавцов
        product_parser.cleanup()
        
        if self.stop_event.is_set():
            return
        
        # Китайского продавца надёжно видно в карточке магазина (описание, адрес,
        # реквизиты), а она доступна только на этапе парсинга продавцов. Поэтому здесь
        # отбрасываем лишь явно российских, чтобы не тратить на них запросы.
        total_products_found = len(product_results)
        if self.settings.CHINA_ONLY:
            product_results = [p for p in product_results if not getattr(p, "is_russian", False)]
            logger.info(
                f"Предфильтр: {len(product_results)} из {total_products_found} товаров "
                f"оставлены (отброшены явно российские продавцы)"
            )
        
        seller_ids = []
        for product in product_results:
            if product.seller_id and product.success:
                seller_ids.append(product.seller_id)
        
        unique_seller_ids = list(set(seller_ids))
        
        seller_results = []
        if unique_seller_ids:
            logger.info(f"Начинаем парсинг {len(unique_seller_ids)} продавцов после закрытия всех воркеров продуктов")
            seller_parser = OzonSellerParser(self.settings.MAX_WORKERS, user_id)
            seller_results = seller_parser.parse_sellers(unique_seller_ids)
            # Закрываем воркеры продавцов после завершения
            seller_parser.cleanup()
        
        if self.stop_event.is_set():
            return
        
        seller_data = {}
        for seller in seller_results:
            if seller.success:
                seller_data[seller.seller_id] = seller

        # Вердикт по карточке магазина важнее предварительного (по товару):
        # переносим его в товар, чтобы он попал и в отчёт, и в дашборд
        for product in product_results:
            seller = seller_data.get(product.seller_id)
            if seller is not None:
                product.is_chinese = seller.is_chinese
                product.chinese_confidence = seller.chinese_confidence
                product.chinese_reasons = seller.chinese_reasons
                product.seller_country = seller.seller_country

        # Окончательный фильтр «только Китай» — по данным карточки магазина
        if self.settings.CHINA_ONLY:
            before = len(product_results)
            kept = [product for product in product_results if product.is_chinese]
            product_results = kept
            logger.info(
                f"Фильтр CHINA_ONLY по магазинам: {len(kept)} из {before} товаров — китайские"
            )
        
        end_time = time.time()
        total_time = end_time - start_time
        successful_products = len([p for p in product_results if p.success])
        failed_products = len([p for p in product_results if not p.success])
        avg_time_per_product = total_time / len(product_results) if product_results else 0
        
        # Сохраняем результаты для конкретного пользователя
        user_results = {
            'links': product_links,
            'products': product_results,
            'sellers': seller_results,
            'category_url': category_url,
            'total_products': len(product_results),
            'successful_products': successful_products,
            'failed_products': failed_products,
            'total_sellers': len(seller_results),
            'successful_sellers': len([s for s in seller_results if s.success]),
            'output_folder': getattr(link_parser, 'output_folder', 'unknown'),
            'seller_data': seller_data,
            'selected_fields': selected_fields,
            'parsing_stats': {
                'total_time': total_time,
                'successful_products': successful_products,
                'failed_products': failed_products,
                'average_time_per_product': avg_time_per_product
            }
        }
        
        # Сохраняем результаты для пользователя
        if user_id:
            self.user_results[user_id] = user_results
        
        # Обновляем глобальные результаты для совместимости
        self.last_results = user_results
        
        self._save_results_to_file(user_id)
        self._export_to_excel(user_id)
        self._send_report_to_telegram(user_id)
    
    def _save_results_to_file(self, user_id: str = None):
        try:
            import json
            from datetime import datetime
            from pathlib import Path
            
            folder_name = self.last_results.get('output_folder', 'unknown')
            filename = f"category_{folder_name}.json"
            current_timestamp = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
            
            output_dir = self.settings.OUTPUT_DIR / folder_name
            filepath = output_dir / filename
            
            # Получаем результаты для конкретного пользователя
            results = self.user_results.get(user_id, self.last_results) if user_id else self.last_results
            
            save_data = {
                'timestamp': current_timestamp,
                'category_url': results.get('category_url', ''),
                'total_products': results.get('total_products', 0),
                'successful_products': results.get('successful_products', 0),
                'total_sellers': results.get('total_sellers', 0),
                'successful_sellers': results.get('successful_sellers', 0),
                'products': []
            }
            
            for product in results.get('products', []):
                product_url = ""
                for url in results.get('links', {}).keys():
                    if product.article in url:
                        product_url = url
                        break
                
                seller_info = results.get('seller_data', {}).get(product.seller_id, None)
                
                seller_data = {
                    'name': product.company_name,
                    'id': product.seller_id,
                    'link': product.seller_link,
                    'inn': '',
                    'company_name': ''
                }
                
                if seller_info:
                    company_name = seller_info.company_name.replace('\\"', '"').replace('\"', '"').replace('"', '"')
                    
                    seller_data.update({
                        'inn': seller_info.inn,
                        'company_name': company_name,
                        'orders_count': seller_info.orders_count,
                        'reviews_count': seller_info.reviews_count,
                        'working_time': seller_info.working_time,
                        'average_rating': seller_info.average_rating
                    })
                
                if 'name' in seller_data:
                    seller_data['name'] = seller_data['name'].replace('\\"', '"').replace('\"', '"').replace('"', '"')
                
                save_data['products'].append({
                    'article': product.article,
                    'name': product.name,
                    'seller': seller_data,
                    'image_url': product.image_url,
                    'card_price': product.card_price,
                    'price': product.price,
                    'original_price': product.original_price,
                    'product_url': product_url,
                    'is_chinese': getattr(product, 'is_chinese', False),
                    'chinese_confidence': getattr(product, 'chinese_confidence', 0.0),
                    'seller_country': getattr(product, 'seller_country', ''),
                    'chinese_reasons': getattr(product, 'chinese_reasons', ''),
                    'success': product.success,
                    'error': product.error
                })
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            logger.error(f"Ошибка сохранения результатов: {e}")
    
    def _export_to_excel(self, user_id: str = None):
        try:
            # Получаем результаты для конкретного пользователя
            results = self.user_results.get(user_id, self.last_results) if user_id else self.last_results
            
            folder_name = results.get('output_folder', 'unknown')
            output_dir = self.settings.OUTPUT_DIR / folder_name
            
            exporter = ExcelExporter(output_dir, f"category_{folder_name}")
            selected_fields = results.get('selected_fields', [])
            
            export_data = {'products': []}
            
            for product in results.get('products', []):
                product_url = ""
                for url in results.get('links', {}).keys():
                    if product.article in url:
                        product_url = url
                        break
                
                seller_info = results.get('seller_data', {}).get(product.seller_id, None)
                
                seller_data = {
                    'name': product.company_name,
                    'inn': '',
                    'company_name': '',
                    'orders_count': '',
                    'reviews_count': '',
                    'average_rating': '',
                    'working_time': ''
                }
                
                if seller_info:
                    seller_data.update({
                        'inn': seller_info.inn,
                        'company_name': seller_info.company_name.replace('\"', '"').replace('"', '"'),
                        'orders_count': seller_info.orders_count,
                        'reviews_count': seller_info.reviews_count,
                        'average_rating': seller_info.average_rating,
                        'working_time': seller_info.working_time
                    })
                
                export_data['products'].append({
                    'article': product.article,
                    'name': product.name,
                    'seller': seller_data,
                    'image_url': product.image_url,
                    'card_price': product.card_price,
                    'price': product.price,
                    'original_price': product.original_price,
                    'product_url': product_url,
                    'is_chinese': getattr(product, 'is_chinese', False),
                    'chinese_confidence': getattr(product, 'chinese_confidence', 0.0),
                    'seller_country': getattr(product, 'seller_country', ''),
                    'chinese_reasons': getattr(product, 'chinese_reasons', ''),
                    'success': product.success,
                    'error': product.error
                })
            
            if exporter.export_results(export_data, selected_fields):
                self._send_files_to_telegram(str(exporter.filepath), user_id)
            
        except Exception as e:
            logger.error(f"Ошибка экспорта в Excel: {e}")
    
    def start_telegram_bot(self, bot_token: str, user_ids) -> bool:
        try:
            if self.telegram_bot:
                self.telegram_bot.stop()
            
            # Поддерживаем как строку, так и массив для обратной совместимости
            if isinstance(user_ids, str):
                user_ids = [user_ids]
            elif not isinstance(user_ids, list):
                user_ids = list(user_ids)
            
            self.telegram_bot = TelegramBotManager(bot_token, user_ids, self)
            return self.telegram_bot.start()
        except Exception as e:
            logger.error(f"Ошибка запуска Telegram бота: {e}")
            return False
    
    def stop_telegram_bot(self):
        if self.telegram_bot:
            self.telegram_bot.stop()
            self.telegram_bot = None
    
    def restart_parsing(self, category_url: str, selected_fields: list = None, user_id: str = None) -> bool:
        self.stop_parsing(user_id)
        time.sleep(1)
        return self.start_parsing(category_url, selected_fields, user_id)
    
    def get_status(self):
        with self.parsing_lock:
            status = {
                'is_running': self.is_running,
                'active_users_count': len(self.active_parsing_users),
                'active_users': list(self.active_parsing_users),
                'telegram_bot_active': self.telegram_bot and hasattr(self.telegram_bot, 'is_running') and self.telegram_bot.is_running,
                'last_results': self.last_results,
                'settings': {
                    'max_products': self.settings.MAX_PRODUCTS,
                    'max_workers': self.settings.MAX_WORKERS
                }
            }
        
        # Добавляем информацию о ресурсах
        resource_status = resource_manager.get_status()
        status.update(resource_status)
        
        return status
    
    def get_user_results(self, user_id: str):
        """Получает результаты парсинга для конкретного пользователя"""
        with self.parsing_lock:
            return self.user_results.get(user_id, None)
    
    def _send_report_to_telegram(self, user_id: str = None):
        self._send_via_temp_bot(report_only=True, target_user_id=user_id)
    
    def _send_files_to_telegram(self, excel_path: str, user_id: str = None):
        self._send_via_temp_bot(excel_path=excel_path, target_user_id=user_id)
    
    def _send_via_temp_bot(self, excel_path: str = None, report_only: bool = False, target_user_id: str = None):
        try:
            from ..utils.config_loader import load_telegram_config
            
            bot_token, config_user_ids = load_telegram_config()
            
            if not bot_token:
                logger.error("Нет TELEGRAM_BOT_TOKEN в config.txt")
                return
            
            # Определяем целевого пользователя
            if target_user_id:
                # Отправляем конкретному пользователю
                target_users = [target_user_id]
            else:
                # Отправляем всем пользователям из конфига (для обратной совместимости)
                if not config_user_ids:
                    logger.error("Нет TELEGRAM_CHAT_ID в config.txt")
                    return
                target_users = config_user_ids.split(',') if isinstance(config_user_ids, str) else [config_user_ids]
            
            from aiogram import Bot
            from aiogram.types import FSInputFile
            
            async def send_files():
                temp_bot = Bot(token=bot_token)
                
                try:
                    for target_user in target_users:
                        target_user = target_user.strip()
                        
                        if report_only:
                            # Получаем результаты для конкретного пользователя
                            results = self.user_results.get(target_user_id, self.last_results) if target_user_id else self.last_results
                            
                            stats = results.get('parsing_stats', {})
                            total_time = stats.get('total_time', 0)
                            successful = stats.get('successful_products', 0)
                            failed = stats.get('failed_products', 0)
                            avg_time = stats.get('average_time_per_product', 0)
                            
                            hours = int(total_time // 3600)
                            minutes = int((total_time % 3600) // 60)
                            seconds = int(total_time % 60)
                            
                            if hours > 0:
                                time_str = f"{hours}ч {minutes}м {seconds}с"
                            elif minutes > 0:
                                time_str = f"{minutes}м {seconds}с"
                            else:
                                time_str = f"{seconds}с"
                            
                            success_rate = (successful / (successful + failed) * 100) if (successful + failed) > 0 else 0
                            
                            report = (
                                "📈 <b>Отчет о парсинге</b>\n\n"
                                f"⏱️ <b>Общее время:</b> {time_str}\n"
                                f"⚡ <b>Среднее время на товар:</b> {avg_time:.1f}с\n\n"
                                f"📦 <b>Всего товаров:</b> {successful + failed}\n"
                                f"✅ <b>Успешно:</b> {successful}\n"
                                f"❌ <b>Неудачно:</b> {failed}\n"
                                f"📊 <b>Успешность:</b> {success_rate:.1f}%"
                            )
                            
                            await temp_bot.send_message(chat_id=target_user, text=report, parse_mode="HTML")
                        
                        if excel_path:
                            caption = (
                                "🎉 <b>Парсинг успешно завершен!</b>\n\n"
                                "📊 <b>Ваш Excel файл готов!</b>\n"
                                "💎 Данные отформатированы и готовы к использованию\n\n"
                                "📥 Скачайте файл ниже ⬇️"
                            )
                            
                            document = FSInputFile(excel_path)
                            await temp_bot.send_document(
                                chat_id=target_user,
                                document=document,
                                caption=caption,
                                parse_mode="HTML"
                            )
                    
                    if excel_path:
                        await asyncio.sleep(10)
                        self._delete_output_folder()
                        
                finally:
                    await temp_bot.session.close()
            
            asyncio.run(send_files())
            
        except Exception as e:
            logger.error(f"Ошибка отправки через временный бот: {e}")
    
    def _delete_output_folder(self):
        try:
            import shutil
            import os
            import stat
            
            folder_name = self.last_results.get('output_folder', '')
            if folder_name:
                output_dir = self.settings.OUTPUT_DIR / folder_name
                if output_dir.exists():
                    def handle_remove_readonly(func, path, exc):
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    
                    shutil.rmtree(output_dir, onerror=handle_remove_readonly)
        except Exception as e:
            logger.error(f"Ошибка удаления папки: {e}")
    
    def shutdown(self):
        # non-blocking wrapper
        threading.Thread(target=self._do_shutdown, daemon=True).start()

    def _do_shutdown(self):
        self.stop_parsing()
        self.stop_telegram_bot()