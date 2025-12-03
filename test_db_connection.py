# test_db_connection.py
"""
Тестовый скрипт проверки подключения к PostgreSQL
"""

import os
import logging
from dotenv import load_dotenv

# Загружаем переменные окружения из .env если есть
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_connection():
    """Проверка подключения к PostgreSQL"""
    
    # Получаем DATABASE_URL
    database_url = os.getenv('HEROKU_POSTGRESQL_COBALT_URL') or os.getenv('DATABASE_URL')
    
    if not database_url:
        logger.error('❌ DATABASE_URL не найден в переменных окружения!')
        logger.info('💡 Для тестирования создайте файл .env с переменными из Heroku')
        return False
    
    # Скрываем пароль в логах
    safe_url = database_url.split('@')[1] if '@' in database_url else 'hidden'
    logger.info(f'🔗 Подключаемся к БД: ...@{safe_url}')
    
    try:
        from sqlalchemy import create_engine, text
        
        # Heroku старый формат postgres:// нужно заменить на postgresql://
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        
        engine = create_engine(database_url, pool_pre_ping=True)
        
        # Тестовый запрос
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version()"))
            version = result.scalar()
            logger.info(f'✅ Подключение успешно!')
            logger.info(f'📊 PostgreSQL версия: {version[:50]}...')
            
        engine.dispose()
        return True
        
    except Exception as e:
        logger.error(f'❌ Ошибка подключения: {e}')
        return False

if __name__ == '__main__':
    print('🔍 Тестирование подключения к PostgreSQL...')
    print('=' * 60)
    success = test_connection()
    print('=' * 60)
    if success:
        print('✅ Подключение работает! Можно запускать миграцию.')
    else:
        print('❌ Подключение не удалось. Проверьте настройки.')
