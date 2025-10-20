# Пример файла конфигурации для Affinia
# Скопируйте этот файл в config.py и заполните своими значениями
# НИКОГДА не коммитьте config.py в Git!

# Telegram Bot Configuration
API_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Получите у @BotFather
BOT_USERNAME = "YOUR_BOT_USERNAME"  # Без @

# Web App URL
WEBAPP_URL = "https://your-app.herokuapp.com"

# Telegram Analytics (DataChief)
TGA_TOKEN = "YOUR_DATA_CHIEF_TOKEN"  # из @DataChief_bot / TON Builders Portal
TGA_APP_NAME = "YOUR_ANALYTICS_IDENTIFIER"  # analytics identifier из @DataChief_bot

# Security Settings
RATE_LIMIT = 100  # Максимум запросов в час
RATE_WINDOW = 3600  # Время окна в секундах

# Logging
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "affinia.log"

# Database (для будущего использования)
DATABASE_URL = "sqlite:///affinia.db"

# Пример использования:
# 1. Скопируйте этот файл: cp config.example.py config.py
# 2. Заполните своими значениями
# 3. Добавьте config.py в .gitignore
# 4. В app.py импортируйте: from config import *
