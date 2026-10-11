"""Environment, logging and constants.

Importing this module loads .env, configures logging and validates the required settings
(API_TOKEN, BOT_USERNAME, WEBAPP_URL); the process exits early if they are missing.
"""
import logging
import os
import sys
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # stdout → Heroku logs; файловая система дайно эфемерна
)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)


class EndpointFilter(logging.Filter):
    """Скрываем из werkzeug-лога частые опросы (они не несут информации)."""
    def filter(self, record):
        msg = record.getMessage()
        return '/get_notifications' not in msg and '/get_new_friends_count' not in msg and '/get_balance' not in msg


logging.getLogger("werkzeug").addFilter(EndpointFilter())

# ---------------------------------------------------------------------------
# Required settings
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv('API_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME')
WEBAPP_URL = os.getenv('WEBAPP_URL')

for _name, _value in (('API_TOKEN', API_TOKEN), ('BOT_USERNAME', BOT_USERNAME), ('WEBAPP_URL', WEBAPP_URL)):
    if not _value:
        logging.error(f'❌ CRITICAL: {_name} environment variable is not set!')
        sys.exit(1)

WEBAPP_URL = WEBAPP_URL.rstrip('/')
URL = WEBAPP_URL  # historical alias used by the bot handlers

# ---------------------------------------------------------------------------
# Optional settings
# ---------------------------------------------------------------------------
ADMIN_ID = os.getenv('ADMIN_ID', '478201816')  # получает уведомление о каждом новом пользователе

# Telegram Apps Center analytics; the SDK is embedded only when both are set
TGA_TOKEN = os.getenv('TGA_TOKEN') or os.getenv('ANALYTICS_TOKEN')
TGA_APP_NAME = os.getenv('TGA_APP_NAME') or os.getenv('ANALYTICS_APP_NAME')

# Соль для псевдонимов в логах. Отдельная переменная, чтобы ротация токена бота не меняла теги.
LOG_SALT = os.getenv('LOG_SALT') or API_TOKEN

# За Heroku-роутером реальный IP клиента — последний элемент X-Forwarded-For. PROXY_HOPS=0 без прокси.
PROXY_HOPS = int(os.getenv('PROXY_HOPS', '1' if os.getenv('DYNO') else '0'))

# Максимальный возраст initData: без этой проверки однажды перехваченная строка работала бы вечно.
INIT_DATA_MAX_AGE = int(os.getenv('INIT_DATA_MAX_AGE', str(24 * 3600)))
CLOCK_SKEW = 300

PRIVACY_CONTACT = os.getenv('PRIVACY_CONTACT', '')
POLICY_UPDATED = '2026-10-10'

# ---------------------------------------------------------------------------
# Limits and TTLs
# ---------------------------------------------------------------------------
# Фронтенд опрашивает /get_notifications каждые 15 с (240/ч), поэтому per-user не ниже ~1000/ч.
# Лимит по IP щадящий: мобильные операторы сажают тысячи людей за один NAT-адрес.
USER_RATE_LIMIT = (1200, 3600)
IP_RATE_LIMIT = (3000, 3600)
AUTH_FAIL_LIMIT = (30, 600)        # неудачных проверок initData с одного IP за 10 минут

INVITE_TTL = {'friend': timedelta(days=30), 'consent': timedelta(days=7)}

AVATAR_CACHE_TTL = 3600
AVATAR_CACHE_MAX = 2000

# Параметры /start, которые НЕ являются реферальными ссылками
RESERVED_START_PAYLOADS = {'', 'webapp', 'appcenter', 'open', 'openapp', 'menu', 'start', 'app'}
