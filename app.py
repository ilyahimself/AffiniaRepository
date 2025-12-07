import os
import sys
import json
import logging
import threading
import asyncio
import qrcode
import hmac
import hashlib
from urllib.parse import parse_qs, unquote
from io import BytesIO
from base64 import b64encode
from flask import Flask, render_template, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot, LabeledPrice
from telegram.ext import (Application, CommandHandler, CallbackContext, CallbackQueryHandler, PreCheckoutQueryHandler, MessageHandler, filters)
from concurrent.futures import TimeoutError
from dotenv import load_dotenv

# Load .env file for local development
load_dotenv()

bot_loop = None

from json.decoder import JSONDecodeError

# PostgreSQL Database Service
try:
    from services.database_service import DatabaseService
    USE_DATABASE = True
except ImportError:
    USE_DATABASE = False
    logging.warning("⚠️ DatabaseService not available, using JSON fallback")

# Настройка логирования с безопасностью
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('affinia.log')
    ]
)

# Отключаем логирование чувствительной информации
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

# Фильтр для логов werkzeug (скрываем частые запросы)
class EndpointFilter(logging.Filter):
    def filter(self, record):
        return "/get_notifications" not in record.getMessage() and \
               "/get_new_friends_count" not in record.getMessage() and \
               "/get_balance" not in record.getMessage()  # Баланс мы логируем сами красиво

logging.getLogger("werkzeug").addFilter(EndpointFilter())

ADMIN_ID = '478201816'

app = Flask(__name__)

# Простой rate limiting
from collections import defaultdict
from time import time

request_counts = defaultdict(list)
RATE_LIMIT = 100  # Максимум 100 запросов
RATE_WINDOW = 3600  # В час (3600 секунд)

def check_rate_limit(user_id):
    """Проверка rate limit для пользователя"""
    now = time()
    user_requests = request_counts[str(user_id)]
    
    # Удаляем старые запросы
    user_requests[:] = [req_time for req_time in user_requests if now - req_time < RATE_WINDOW]
    
    if len(user_requests) >= RATE_LIMIT:
        return False
    
    user_requests.append(now)
    return True


# Rate limiting по IP адресу (защита от DDoS и брутфорса)
ip_request_counts = defaultdict(list)
IP_RATE_LIMIT = 200  # Максимум 200 запросов с одного IP
IP_RATE_WINDOW = 3600  # В час

def check_ip_rate_limit(ip_address: str) -> bool:
    """Проверка rate limit для IP адреса"""
    if not ip_address:
        return True
    now = time()
    ip_requests = ip_request_counts[ip_address]
    
    # Удаляем старые запросы
    ip_requests[:] = [req_time for req_time in ip_requests if now - req_time < IP_RATE_WINDOW]
    
    if len(ip_requests) >= IP_RATE_LIMIT:
        logging.warning(f'🛡️ IP rate limit exceeded: {ip_address}')
        return False
    
    ip_requests.append(now)
    return True


def validate_init_data(init_data: str, bot_token: str) -> dict:
    """
    Валидация Telegram WebApp initData с использованием HMAC-SHA256.
    Возвращает данные пользователя или None если валидация не прошла.
    """
    if not init_data:
        return None
    
    try:
        # Парсим строку init_data
        parsed = dict(parse_qs(init_data, keep_blank_values=True))
        parsed = {k: v[0] for k, v in parsed.items()}
        
        # Извлекаем и удаляем hash
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            logging.warning('🛡️ initData validation failed: no hash')
            return None
        
        # Создаём data-check-string (отсортированные ключи)
        data_check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        
        # Вычисляем secret key
        secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode(),
            hashlib.sha256
        ).digest()
        
        # Вычисляем hash
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Проверяем соответствие
        if not hmac.compare_digest(computed_hash, received_hash):
            logging.warning(f'🛡️ initData validation failed: hash mismatch')
            return None
        
        # Парсим JSON с данными пользователя
        user_json = unquote(parsed.get('user', '{}'))
        user_data = json.loads(user_json)
        
        return {
            'user_id': str(user_data.get('id', '')),
            'username': user_data.get('username', ''),
            'first_name': user_data.get('first_name', ''),
            'last_name': user_data.get('last_name', ''),
            'language_code': user_data.get('language_code', ''),
            'is_premium': user_data.get('is_premium', False),
            'auth_date': parsed.get('auth_date', ''),
        }
    except Exception as e:
        logging.error(f'🛡️ initData validation error: {e}')
        return None


def get_validated_user(data: dict) -> dict:
    """
    Извлекает и валидирует пользователя из запроса.
    Возвращает данные пользователя или None если валидация не прошла.
    """
    init_data = data.get('init_data', '')
    
    # Валидируем initData
    user_data = validate_init_data(init_data, API_TOKEN)
    
    if user_data and user_data.get('user_id'):
        return user_data
    
    # Fallback для бота и внутренних вызовов (без initData)
    # Для внешних API запросов возвращаем None
    return None

user_tokens = {}
referrals = {}
beloveds = {}
user_stars = {}
user_profiles = {}
pending_friend_requests = {}  # Хранение ожидающих запросов дружбы
pending_consent_requests = {}  # Хранение ожидающих запросов партнерства
user_notifications = {}  # Хранение уведомлений для пользователей
new_friends_count = {}  # Счетчик новых непросмотренных друзей
new_partners_count = {}  # Счетчик новых непросмотренных партнеров
consent_history = {}  # История всех consent связей
data_lock = threading.Lock()

# Зарезервированные параметры /start, которые НЕ являются рефералами
RESERVED_START_PAYLOADS = {
    '',
    'webapp', 
    'appcenter',
    'open',
    'openapp',
    'menu',
    'start',
    'app'
}

def is_reserved_start_payload(arg: str) -> bool:
    """Проверяет, является ли параметр зарезервированным (не реферальным)"""
    if arg is None:
        return True
    norm = arg.strip().lower()
    if not norm:
        return True
    # Проверяем точное совпадение или префикс
    return norm in RESERVED_START_PAYLOADS or norm.startswith('webapp') or norm.startswith('appcenter')

def is_valid_referrer(referrer_id: str, user_id: str) -> bool:
    """Проверяет, является ли referrer_id валидным ID пользователя"""
    if not referrer_id or not referrer_id.isdigit():
        return False
    if referrer_id == user_id:
        return False
    return referrer_id in user_tokens


def get_user_display_name(user_id: str) -> str:
    """Получает отображаемое имя пользователя (никнейм или имя)"""
    if not user_id:
        return "Unknown"
    
    profile = user_profiles.get(user_id, {})
    username = profile.get('username', '')
    first_name = profile.get('first_name', '')
    last_name = profile.get('last_name', '')
    
    if username:
        return f"@{username} (ID: {user_id})"
    elif first_name or last_name:
        full_name = f"{first_name} {last_name}".strip()
        return f"{full_name} (ID: {user_id})"
    else:
        return f"User {user_id}"


def ensure_user_record(user_id: str) -> bool:
    """Гарантирует, что пользователь есть в user_tokens (и PostgreSQL), даже с нулевым балансом."""
    if not user_id:
        return False

    created = False
    
    # Check/create in PostgreSQL first
    if USE_DATABASE:
        try:
            user, was_created = DatabaseService.ensure_user(user_id)
            created = was_created
        except Exception as e:
            logging.error(f"Database error in ensure_user_record: {e}")
    
    # Also maintain local cache
    with data_lock:
        if user_id not in user_tokens:
            user_tokens[user_id] = 0
            if not USE_DATABASE:
                created = True

    if created:
        display_name = get_user_display_name(user_id)
        logging.info(f'🆕 NEW USER REGISTERED: {display_name} with 0 AFN')
        save_data()

        # Отправка уведомления админу
        if ADMIN_ID and user_id != ADMIN_ID:
            try:
                msg = f"🆕 <b>NEW USER REGISTERED!</b>\n\n👤 <b>User:</b> {display_name}\n🆔 <b>ID:</b> <code>{user_id}</code>"
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                if loop.is_running():
                    loop.create_task(bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='HTML'))
                else:
                    loop.run_until_complete(bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='HTML'))
            except Exception as e:
                logging.error(f"Failed to send admin notification: {e}")

    return created


def update_user_profile(tg_user) -> None:
    """Сохраняет ник/имя пользователя для отображения вместо сухих ID."""
    if tg_user is None:
        return

    user_id = str(getattr(tg_user, 'id', ''))
    if not user_id:
        return

    profile = {
        'username': tg_user.username or '',
        'first_name': tg_user.first_name or '',
        'last_name': tg_user.last_name or '',
        'language_code': getattr(tg_user, 'language_code', '') or '',
        'is_premium': bool(getattr(tg_user, 'is_premium', False))
    }

    # Update in PostgreSQL first
    if USE_DATABASE:
        try:
            DatabaseService.update_profile(
                user_id,
                username=profile['username'],
                first_name=profile['first_name'],
                last_name=profile['last_name'],
                language_code=profile['language_code'],
                is_premium=profile['is_premium']
            )
        except Exception as e:
            logging.error(f"Database error updating profile: {e}")

    # Check if profile was empty before
    is_new_profile = user_id not in user_profiles or not user_profiles[user_id]

    updated = False
    with data_lock:
        if user_profiles.get(user_id) != profile:
            user_profiles[user_id] = profile
            updated = True

    if updated:
        logging.info(f'👤 Stored profile for user {user_id}: @{profile["username"] or "unknown"}')
        save_data()

# Безопасная загрузка конфигурации
API_TOKEN = os.getenv('API_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME')
WEBAPP_URL = os.getenv('WEBAPP_URL')
# Telegram Analytics (DataChief) configuration
TGA_TOKEN = os.getenv('TGA_TOKEN') or os.getenv('ANALYTICS_TOKEN')
TGA_APP_NAME = os.getenv('TGA_APP_NAME') or os.getenv('ANALYTICS_APP_NAME')
USE_WEBHOOK = os.getenv('USE_WEBHOOK', 'False').lower() == 'true'  # Переключатель webhook

# Проверка обязательных переменных окружения
if not API_TOKEN:
    logging.error('❌ CRITICAL: API_TOKEN environment variable is not set!')
    logging.error('Please set API_TOKEN in your environment variables.')
    sys.exit(1)

if not BOT_USERNAME:
    logging.error('❌ CRITICAL: BOT_USERNAME environment variable is not set!')
    logging.error('Please set BOT_USERNAME in your environment variables.')
    sys.exit(1)

if not WEBAPP_URL:
    logging.warning('⚠️ WARNING: WEBAPP_URL not set, using default Heroku URL')
    WEBAPP_URL = "https://affinia.herokuapp.com"

# Инициализация бота с проверкой токена
try:
    bot = Bot(token=API_TOKEN)
    application = Application.builder().token(API_TOKEN).connection_pool_size(50).build()
    logging.info(f'✅ Bot initialized successfully with username: @{BOT_USERNAME}')
except Exception as e:
    logging.error(f'❌ Failed to initialize bot: {e}')
    sys.exit(1)

URL = WEBAPP_URL


def init_database():
    """Initialize PostgreSQL database connection"""
    global USE_DATABASE
    if not USE_DATABASE:
        return False
    
    db_url = os.getenv('HEROKU_POSTGRESQL_COBALT_URL') or os.getenv('DATABASE_URL')
    if db_url:
        success = DatabaseService.initialize(db_url)
        if success:
            logging.info("✅ PostgreSQL database initialized")
            return True
        else:
            logging.error("❌ Failed to initialize PostgreSQL, falling back to JSON")
            USE_DATABASE = False
            return False
    else:
        logging.warning("⚠️ No database URL found, using JSON storage")
        USE_DATABASE = False
        return False


def load_data():
    global user_tokens, referrals, beloveds, user_stars, user_profiles
    global pending_friend_requests, pending_consent_requests
    global user_notifications, new_friends_count, new_partners_count, consent_history
    
    # Try to initialize database first
    init_database()
    
    # If using PostgreSQL, load from there
    if USE_DATABASE:
        try:
            # Load users from PostgreSQL
            db_users = DatabaseService.get_all_users()
            if db_users:
                user_tokens = db_users
                logging.info(f"✅ Loaded {len(db_users)} users from PostgreSQL")
            else:
                user_tokens = {}
            
            # Load relationships
            referrals = {}
            beloveds = {}
            user_profiles = {}
            
            # For each user, get their friends and partners
            for user_id in user_tokens.keys():
                friends = DatabaseService.get_friends(user_id)
                if friends:
                    referrals[user_id] = friends
                
                partners = DatabaseService.get_partners(user_id)
                if partners:
                    beloveds[user_id] = partners
                
                # Get profile
                user = DatabaseService.get_user(user_id)
                if user and (user.username or user.first_name):
                    user_profiles[user_id] = {
                        'username': user.username or '',
                        'first_name': user.first_name or '',
                        'last_name': user.last_name or '',
                        'language_code': user.language_code or '',
                        'is_premium': user.is_premium or False
                    }
            
            # Initialize other dicts
            user_stars = {}
            pending_friend_requests = {}
            pending_consent_requests = {}
            user_notifications = {}
            new_friends_count = {}
            new_partners_count = {}
            consent_history = {}
            logging.info(f"✅ Loaded data from PostgreSQL: {len(user_tokens)} users")
            return
        except Exception as e:
            logging.error(f"Error loading from PostgreSQL: {e}, falling back to JSON")
    
    # Fallback to JSON file
    try:
        with open('user_tokens.json', 'r') as f:
            data = json.load(f)
            user_tokens = data.get('user_tokens', {})
            referrals = data.get('referrals', {})
            beloveds = data.get('beloveds', {})
            user_stars = data.get('user_stars', {})
            user_profiles = data.get('user_profiles', {})
            pending_friend_requests = data.get('pending_friend_requests', {})
            pending_consent_requests = data.get('pending_consent_requests', {})
            user_notifications = data.get('user_notifications', {})
            new_friends_count = data.get('new_friends_count', {})
            new_partners_count = data.get('new_partners_count', {})
            consent_history = data.get('consent_history', {})
            print(f"Loaded data from JSON: {len(user_tokens)} users")
    except FileNotFoundError:
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}
        user_profiles = {}
        pending_friend_requests = {}
        pending_consent_requests = {}
        user_notifications = {}
        new_friends_count = {}
        new_partners_count = {}
        consent_history = {}
        print("Data file not found, starting with empty dictionary")
    except JSONDecodeError:
        logging.error('Error decoding JSON from user_tokens.json. Starting with empty data.')
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}
        user_profiles = {}
    except Exception as e:
        logging.error(f"Unexpected error while loading data: {e}")
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}
        user_profiles = {}

def save_data():
    """Save data to PostgreSQL and JSON file"""
    # Save to JSON as backup
    try:
        with data_lock:
            with open('user_tokens.json', 'w') as f:
                json.dump({
                    'user_tokens': user_tokens,
                    'referrals': referrals,
                    'beloveds': beloveds,
                    'user_stars': user_stars,
                    'user_profiles': user_profiles,
                    'pending_friend_requests': pending_friend_requests,
                    'pending_consent_requests': pending_consent_requests,
                    'user_notifications': user_notifications,
                    'new_friends_count': new_friends_count,
                    'new_partners_count': new_partners_count,
                    'consent_history': consent_history
                }, f)
        logging.debug(f"Saved data to JSON: {len(user_tokens)} users")
    except Exception as e:
        logging.error(f"Error saving to JSON: {e}")
    
    # PostgreSQL is updated in real-time via DatabaseService methods
    # No need to sync here as we update DB directly in ensure_user_record, etc.


# Flask-маршруты
@app.route('/mini_app')
def mini_app():
    return render_template(
        'index.html',
        analytics_token=TGA_TOKEN,
        analytics_app_name=TGA_APP_NAME,
        bot_username=BOT_USERNAME,
        tg_startapp_link=f"https://t.me/{BOT_USERNAME}?startapp"
    )


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')


@app.route('/static/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/json')


@app.route('/static/<path:filename>')
def static_files(filename):
    # Let Flask infer the correct mime type for static assets
    return send_from_directory('static', filename)

@app.route('/')
def index():
    return render_template(
        'index.html',
        analytics_token=TGA_TOKEN,
        analytics_app_name=TGA_APP_NAME,
        bot_username=BOT_USERNAME,
        tg_startapp_link=f"https://t.me/{BOT_USERNAME}?startapp"
    )

@app.route('/tonconnect-manifest.json')
def tonconnect_manifest():
    # TON Connect manifest for wallet integration
    manifest = {
        "url": WEBAPP_URL,
        "name": "Affinia",
        "iconUrl": f"{WEBAPP_URL}/static/icon-192x192.png"
    }
    return jsonify(manifest)

@app.route('/test-manifest')
def test_manifest():
    return "Manifest test works! Version 2.0"


# Migration endpoint removed for security (migration already completed)
# If needed in future, use Heroku CLI: heroku run python -c "from services.database_service import DatabaseService; ..."


@app.route('/get_balance', methods=['POST'])
def get_balance():
    data = request.json or {}
    
    # IP Rate limiting (защита от DDoS)
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()
    if not check_ip_rate_limit(client_ip):
        return jsonify({'error': 'Too many requests. Please try again later.'}), 429
    
    # Валидация initData от Telegram (КРИТИЧНО для безопасности!)
    validated_user = get_validated_user(data)
    if not validated_user:
        logging.warning(f'🛡️ Unauthorized /get_balance attempt from IP: {client_ip}')
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    
    # Используем данные профиля из валидированного initData
    profile = {
        'username': validated_user.get('username', ''),
        'first_name': validated_user.get('first_name', ''),
        'last_name': validated_user.get('last_name', ''),
        'language_code': validated_user.get('language_code', ''),
        'is_premium': validated_user.get('is_premium', False)
    }
    
    # Сохраняем профиль
    with data_lock:
        if user_id not in user_profiles or user_profiles.get(user_id) != profile:
            user_profiles[user_id] = profile
            logging.info(f'👤 Profile from Mini App: {user_id} → @{profile["username"] or "unknown"}')
    
    # Сохраняем в PostgreSQL
    if USE_DATABASE:
        try:
            DatabaseService.update_profile(
                user_id,
                username=profile['username'],
                first_name=profile['first_name'],
                last_name=profile['last_name'],
                language_code=profile['language_code'],
                is_premium=profile['is_premium']
            )
        except Exception as e:
            logging.error(f"DB error saving profile: {e}")
    
    # Создаём запись пользователя
    ensure_user_record(user_id)
    
    # Rate limiting по user_id
    if not check_rate_limit(user_id):
        logging.warning(f'Rate limit exceeded for user {user_id}')
        return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429
    
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    
    display_name = get_user_display_name(user_id)
    logging.info(f'💰 Balance check: {display_name} → {balance} AFN')
    return jsonify({'balance': balance})


@app.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json or {}
    
    # IP Rate limiting
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()
    if not check_ip_rate_limit(client_ip):
        return jsonify({'error': 'Too many requests'}), 429
    
    # Валидация initData
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
    if not check_rate_limit(user_id):
        return jsonify({'error': 'Rate limit exceeded'}), 429
    
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    display_name = get_user_display_name(user_id)
    logging.info(f'🔗 Referral link created: {display_name}')
    return jsonify({'link': referral_link})


@app.route('/generate_referral_qr', methods=['POST'])
def generate_referral_qr():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    # Генерация QR-кода
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(referral_link)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    qr_code_base64 = b64encode(buffered.getvalue()).decode('utf-8')
    return jsonify({'referral_link': referral_link, 'qr_code': qr_code_base64})


@app.route('/create_consent_link', methods=['POST'])
def create_consent_link():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
    return jsonify({'link': consent_link})


@app.route('/generate_consent_qr', methods=['POST'])
def generate_consent_qr():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    ensure_user_record(user_id)
    # Ссылка ведёт прямо в мини-приложение с GET-параметром consent
    consent_link = f"{URL}/mini_app?consent={user_id}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(consent_link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    qr_code_base64 = b64encode(buffered.getvalue()).decode('utf-8')
    return jsonify({'consent_link': consent_link, 'qr_code': qr_code_base64})


@app.route('/consent_response', methods=['POST'])
def consent_response():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    new_user_id = validated_user['user_id']
    
    if 'referrer_id' not in data or 'decision' not in data:
        return jsonify({'error': 'Missing parameters'}), 400

    referrer_id = str(data['referrer_id'])
    ensure_user_record(referrer_id)
    ensure_user_record(new_user_id)
    decision = data['decision']

    if decision == 'consent':
        if referrer_id not in beloveds:
            beloveds[referrer_id] = []
        if new_user_id not in beloveds:
            beloveds[new_user_id] = []
        if new_user_id not in beloveds[referrer_id]:
            beloveds[referrer_id].append(new_user_id)
        if referrer_id not in beloveds[new_user_id]:
            beloveds[new_user_id].append(referrer_id)

        save_data()

        # Logging
        referrer_display = get_user_display_name(referrer_id)
        new_user_display = get_user_display_name(new_user_id)
        logging.info(f'❤️ CONSENT ACCEPTED: {new_user_display} accepted request from {referrer_display}')

        # Отправляем уведомление отправителю через бота, чтобы он знал, что связь установлена
        try:
            bot.send_message(
                chat_id=referrer_id,
                text='Your consent request has been accepted. Check your Beloved page for the new connection.'
            )
        except Exception as e:
            logging.error(f"Error sending bot message: {e}")
        return jsonify({'success': True})
    elif decision == 'decline':
        # Logging
        referrer_display = get_user_display_name(referrer_id)
        new_user_display = get_user_display_name(new_user_id)
        logging.info(f'💔 CONSENT DECLINED: {new_user_display} declined request from {referrer_display}')
        try:
            bot.send_message(chat_id=referrer_id, text='Your consent request has been declined.')
        except Exception as e:
            logging.error(f"Error sending message: {e}")
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Invalid decision'}), 400


@app.route('/get_beloved', methods=['POST'])
def get_beloved():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    if user_id not in beloveds:
        return jsonify({'beloved': []})
    
    # Создаем полную структуру данных для партнеров
    beloved_data = []
    for b_id in beloveds[user_id]:
        # Определяем статус отношений на основе consent_history
        relationship_status = 'potential'  # по умолчанию
        
        if user_id in consent_history:
            for consent in consent_history[user_id]:
                if consent.get('partner_id') == b_id and consent.get('status') == 'active':
                    relationship_status = 'current'
                    break
        
        partner_data = {
            "user_id": b_id,
            "relationship_status": relationship_status,
            "trust_level": "partner",
            "added_date": int(time()),  # временная метка
            "consent_type": "intimate"
        }
        beloved_data.append(partner_data)
    
    return jsonify({'beloved': beloved_data})


@app.route('/delete_beloved', methods=['POST'])
def delete_beloved():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    if 'beloved_id' not in data:
        return jsonify({'error': 'beloved_id is required'}), 400
    beloved_id = str(data['beloved_id'])
    ensure_user_record(user_id)
    ensure_user_record(beloved_id)

    if user_id in beloveds and beloved_id in beloveds[user_id]:
        beloveds[user_id].remove(beloved_id)
        if beloved_id in beloveds and user_id in beloveds[beloved_id]:
            beloveds[beloved_id].remove(user_id)
        save_data()
        return jsonify({'success': True})
    return jsonify({'error': 'Beloved not found'}), 400

@app.route('/get_donate_link', methods=['POST'])
def get_donate_link():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
    # Rate limiting для донатов (более строгий)
    if not check_rate_limit(f"donate_{user_id}"):
        logging.warning(f'Donation rate limit exceeded for user {user_id}')
        return jsonify({'error': 'Too many donation requests. Please wait before trying again.'}), 429
    
    # Получаем сумму из запроса или используем 100 по умолчанию
    amount = data.get('amount', 100)
    
    # Валидация суммы
    if not isinstance(amount, int) or amount < 50 or amount > 10000:
        logging.warning(f'Invalid donation amount from user {user_id}: {amount}')
        return jsonify({'error': 'Amount must be between 50 and 10000 stars'}), 400

    # Создаем описание в зависимости от суммы
    if amount <= 100:
        tier = "Basic Support"
    elif amount <= 500:
        tier = "Premium Support"
    elif amount <= 1000:
        tier = "VIP Support"
    else:
        tier = "Elite Support"

    # Запускаем корутину в event loop, сохранённом в bot_loop
    future = asyncio.run_coroutine_threadsafe(
        bot.create_invoice_link(
            title=f"Support Affinia - {tier}",
            description=f"Thank you for supporting our app with {amount} Telegram Stars! 💖",
            payload=f"donation_{user_id}_{amount}",
            provider_token="",  # Для Telegram Stars оставляем пустым
            currency="XTR",     # Валюта Telegram Stars
            prices=[LabeledPrice(label=f"{tier} ({amount} Stars)", amount=amount)]
        ),
        bot_loop  # Используем сохранённый event loop
    )

    try:
        invoice_link = future.result(timeout=10)
        logging.info(f'Donation invoice created for user {user_id}: {amount} stars, tier: {tier}')
        return jsonify({'success': True, 'invoice_link': invoice_link})
    except Exception as e:
        logging.error(f"Error creating invoice for user {user_id}: {e}")
        return jsonify({'error': f'Failed to create donation invoice: {e}'}), 500

# ===== Хендлеры бота =====

async def start(update: Update, context: CallbackContext) -> None:
    args = context.args
    user = update.message.from_user
    user_id = str(user.id)
    # ВАЖНО: Сначала сохраняем профиль, потом создаём запись пользователя
    # Это нужно чтобы в уведомлении о новом пользователе был никнейм
    update_user_profile(user)
    ensure_user_record(user_id)
    
    # Логируем вход пользователя
    display_name = get_user_display_name(user_id)
    logging.info(f'👤 User started bot: {display_name}')
    
    if args:
        arg = args[0]
        
        # ВАЖНО: Проверяем, не является ли это зарезервированным параметром
        if is_reserved_start_payload(arg):
            # Это системный параметр (webapp, appcenter и т.д.) - просто показываем меню
            logging.info(f'User {user_id} started with reserved payload: {arg}')
            await show_menu(update, context, user_id)
            return
        
        # Проверяем тип приглашения
        if arg.startswith("friend_"):
            # Это приглашение в друзья - открываем мини-апп с параметром
            await handle_friend_invitation(update, context, arg)
        elif arg.startswith("consent_"):
            # Проверяем формат - новый или старый
            parts = arg.split("_")
            if len(parts) >= 3:  # Новый формат: consent_userId_timestamp
                await handle_consent_invitation(update, context, arg)
            else:  # Старый формат: consent_userId
                await handle_consent(update, context, parts[1])
        elif arg.isdigit():
            # Это потенциально реферальная ссылка (только числовые ID)
            if is_valid_referrer(arg, user_id):
                await referral_start(update, context, arg)
            else:
                # ID числовой, но пользователь не существует или это сам пользователь
                logging.warning(f'Invalid referral link (user not found): {arg} for user {user_id}')
                await update.message.reply_text("Invalid referral link.")
                await show_menu(update, context, user_id)
        else:
            # Параметр не числовой и не friend_/consent_ -> считаем системным
            logging.info(f'User {user_id} started with non-numeric parameter (treated as reserved): {arg}')
            await show_menu(update, context, user_id)
    else:
        await show_menu(update, context, user_id)


async def referral_start(update: Update, context: CallbackContext, referrer_id: str) -> None:
    new_user = update.message.from_user
    new_user_id = str(new_user.id)
    update_user_profile(new_user)
    ensure_user_record(referrer_id)
    ensure_user_record(new_user_id)

    if not referrer_id:
        await update.message.reply_text("Invalid referral link.")
        await show_menu(update, context, new_user_id)
        return

    if referrer_id == new_user_id:
        error_text = 'You cannot use your own referral link.'
        await update.message.reply_text(error_text)
    else:
        if referrer_id not in referrals:
            referrals[referrer_id] = []

        if new_user_id not in referrals[referrer_id]:
            # Добавляем нового пользователя в список друзей рефери
            referrals[referrer_id].append(new_user_id)

            # Обратная связь: добавляем рефери в список друзей нового пользователя
            if new_user_id not in referrals:
                referrals[new_user_id] = []
            if referrer_id not in referrals[new_user_id]:
                referrals[new_user_id].append(referrer_id)

            user_tokens[referrer_id] = user_tokens.get(referrer_id, 0) + 10
            user_tokens[new_user_id] = user_tokens.get(new_user_id, 0) + 5
            save_data()
            
            # Логирование реферальной регистрации
            referrer_display = get_user_display_name(referrer_id)
            new_user_display = get_user_display_name(new_user_id)
            logging.info(f'🎉 REFERRAL SUCCESS! {new_user_display} invited by {referrer_display} | Rewards: +10 AFN (referrer), +5 AFN (new user)')

            referrer_text = 'You have been credited with 10 AffinyCoin for inviting a new user.'
            new_user_text = ('Thank you for registering! You have been credited with 5 AffinyCoin.\n'
                             'Your referrer has been credited with 10 AffinyCoin.')
            await update.message.reply_text(new_user_text)
            await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
        else:
            error_text = "You have already registered using a referral link."
            await update.message.reply_text(error_text)

    await show_menu(update, context, new_user_id)


async def handle_consent(update: Update, context: CallbackContext, referrer_id: str) -> None:
    new_user = update.message.from_user
    new_user_id = str(new_user.id)
    update_user_profile(new_user)
    ensure_user_record(new_user_id)
    ensure_user_record(referrer_id)
    # Формируем URL мини-приложения с GET-параметром consent,
    # где referrer_id – это ID пользователя, отправившего consent-запрос.
    open_app_url = f"{URL}/mini_app?consent={referrer_id}"

    keyboard = [
        [InlineKeyboardButton("Open App", web_app=WebAppInfo(url=open_app_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "To respond to the consent request, please open the app."
    await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_friend_invitation(update: Update, context: CallbackContext, invite_id: str) -> None:
    """Обработка приглашения в друзья"""
    user = update.message.from_user
    user_id = str(user.id)
    inviter_id = invite_id.split('_')[1] if '_' in invite_id else ''
    update_user_profile(user)
    ensure_user_record(user_id)
    ensure_user_record(inviter_id)
    
    # Формируем URL мини-приложения с параметром приглашения
    open_app_url = f"{URL}/mini_app?invite={invite_id}"
    
    text = f"🌟 You've been invited to become friends in Affinia!\n\n" \
           f"Click the button below to open the app and respond to the invitation."
    
    keyboard = [
        [InlineKeyboardButton("🚀 Open Affinia", web_app=WebAppInfo(url=open_app_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_consent_invitation(update: Update, context: CallbackContext, invite_id: str) -> None:
    """Обработка приглашения для партнерства"""
    user = update.message.from_user
    user_id = str(user.id)
    inviter_id = invite_id.split('_')[1] if '_' in invite_id else ''
    update_user_profile(user)
    ensure_user_record(user_id)
    ensure_user_record(inviter_id)
    
    # Формируем URL мини-приложения с параметром приглашения
    open_app_url = f"{URL}/mini_app?invite={invite_id}"
    
    text = f"💕 You've been invited to establish an intimate connection in Affinia!\n\n" \
           f"This is a special invitation to become partners. " \
           f"Click the button below to open the app and respond."
    
    keyboard = [
        [InlineKeyboardButton("💕 Open Affinia", web_app=WebAppInfo(url=open_app_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)


async def show_menu(update, context, user_id=None):
    tg_user = None
    if hasattr(update, 'message') and getattr(update, 'message', None):
        tg_user = getattr(update.message, 'from_user', None)
    elif hasattr(update, 'from_user'):
        tg_user = update.from_user
    elif hasattr(update, 'callback_query') and getattr(update, 'callback_query', None):
        tg_user = update.callback_query.from_user

    if user_id is None:
        if tg_user is None:
            logging.warning('show_menu called without user context')
            return
        user_id = str(tg_user.id)

    ensure_user_record(user_id)
    if tg_user:
        update_user_profile(tg_user)
    ensure_user_record(user_id)

    keyboard = [
        [InlineKeyboardButton("My Balance", callback_data='balance')],
        [InlineKeyboardButton("Referral Link", callback_data='create_referral')],
        [InlineKeyboardButton("Consent Link", callback_data='create_consent')],
        [InlineKeyboardButton("Open App", web_app=WebAppInfo(url=f"{URL}/mini_app"))]
    ]

    text = 'Please choose:'
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update, 'message'):
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.edit_message_text(text, reply_markup=reply_markup)


async def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    command = query.data
    user_id = str(query.from_user.id)
    update_user_profile(query.from_user)

    if command == 'balance':
        await balance(query, context, callback_query=True)
    elif command == 'create_referral':
        await create_referral_link_btn(query, context)
    elif command == 'create_consent':
        await create_consent_link_btn(query, context)
    elif command.startswith('consent_'):
        await process_consent(query, context, command)
    elif command.startswith('decline_'):
        await process_decline(query, context, command)


async def balance(update: Update, context: CallbackContext, callback_query=False) -> None:
    tg_user = update.message.from_user if not callback_query else update.from_user
    user_id = str(tg_user.id)
    update_user_profile(tg_user)
    ensure_user_record(user_id)
    bal = user_tokens.get(user_id, 0)
    text = f'Your balance: {bal} AffinyCoin.'
    if callback_query:
        await update.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    await show_menu(update, context, user_id)


async def create_referral_link_btn(update: Update, context: CallbackContext) -> None:
    user = update.from_user
    user_id = str(user.id)
    update_user_profile(user)
    ensure_user_record(user_id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = f'Share this link to invite friends and earn tokens: {referral_link}'
    await update.edit_message_text(text)
    await show_menu(update, context, user_id)


async def create_consent_link_btn(update: Update, context: CallbackContext) -> None:
    user = update.from_user
    user_id = str(user.id)
    update_user_profile(user)
    ensure_user_record(user_id)
    # Больше не списываем токены за создание consent link
    consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
    text = f'Share this link to request consent: {consent_link}'
    await update.edit_message_text(text)
    await show_menu(update, context, user_id)


async def process_consent(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
    update_user_profile(query.from_user)
    ensure_user_record(referrer_id)
    ensure_user_record(new_user_id)
    referrer_text = 'Congratulations, you have been given consent for intimacy.'
    await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    await query.edit_message_text(text="You have given consent.")
    await show_menu(query, context, new_user_id)


async def process_decline(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
    update_user_profile(query.from_user)
    ensure_user_record(referrer_id)
    ensure_user_record(new_user_id)
    referrer_text = 'Sorry, you have been declined.'
    await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    await query.edit_message_text(text="You have declined consent.")
    await show_menu(query, context, new_user_id)


# Инициализация обработчиков (общая для polling и webhook)
def setup_handlers():
    """Настройка обработчиков бота"""
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("donate", send_invoice))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    logging.info('✅ Bot handlers registered')


# Polling mode (текущий рабочий способ)
def run_bot_polling():
    """Запуск бота в режиме polling"""
    global bot_loop
    # Создаем новый event loop для polling (современный способ)
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    
    setup_handlers()
    logging.info('🔄 Starting bot in POLLING mode...')
    application.run_polling()


# Webhook mode (для будущего использования)
async def run_bot_webhook():
    """Запуск бота в режиме webhook"""
    setup_handlers()
    
    # Удаляем старый webhook (на всякий случай)
    await remove_webhook()
    
    # Устанавливаем новый webhook
    await setup_webhook()
    
    logging.info('🌐 Bot started in WEBHOOK mode')

async def send_invoice(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    chat_id = update.message.chat_id
    update_user_profile(user)
    ensure_user_record(str(user.id))
    title = "Support the App"
    description = "Donate using Telegram Stars!"
    payload = f"donation_{chat_id}"
    currency = "XTR"  # Валюта Telegram Stars
    prices = [LabeledPrice(label="Telegram Stars Donation", amount=100)]  # 100 Звёзд
    
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",  # Оставляем пустым для Telegram Stars
        currency=currency,
        prices=prices,
        start_parameter="donate",
        need_name=True,
        need_email=False,
        is_flexible=False,
    )

async def precheckout_callback(update: Update, context: CallbackContext) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("donation_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Something went wrong. Try again!")

async def successful_payment_callback(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    user = update.message.from_user
    user_id = str(user.id)
    update_user_profile(user)
    ensure_user_record(user_id)
    
    # Получаем сумму из payload
    payload = update.message.successful_payment.invoice_payload
    try:
        # Payload формата: "donation_userid_amount"
        parts = payload.split('_')
        if len(parts) >= 3:
            amount = int(parts[2])
        else:
            amount = 100  # Значение по умолчанию для старых платежей
    except (ValueError, IndexError):
        amount = 100

    # Обновляем баланс звёзд пользователя
    user_stars[user_id] = user_stars.get(user_id, 0) + amount
    save_data()

    # Отправляем персонализированное сообщение
    if amount >= 1000:
        message = f"🌟 WOW! Thank you for your amazing {amount} stars donation! You're now our VIP supporter! 💎"
    elif amount >= 500:
        message = f"⭐ Thank you for your generous {amount} stars donation! You're awesome! 🎉"
    else:
        message = f"💖 Thank you for your {amount} stars donation! Every star helps us improve! ✨"

    await update.message.reply_text(message)


@app.route('/get_friends', methods=['POST'])
def get_friends():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    if user_id not in referrals:
        return jsonify({'friends': []})

    friends_data = [{"user_id": friend_id, "coins": user_tokens.get(friend_id, 0)} for friend_id in referrals[user_id]]

    return jsonify({'friends': friends_data})


@app.route('/delete_friend', methods=['POST'])
def delete_friend():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    if 'friend_id' not in data:
        return jsonify({'error': 'friend_id is required'}), 400
    friend_id = str(data['friend_id'])
    ensure_user_record(user_id)
    ensure_user_record(friend_id)

    if user_id in referrals and friend_id in referrals[user_id]:
        # Удаляем друга у пользователя
        referrals[user_id].remove(friend_id)
        # Удаляем пользователя у друга, если связь существует
        if friend_id in referrals and user_id in referrals[friend_id]:
            referrals[friend_id].remove(user_id)
        save_data()
        return jsonify({'success': True})

    return jsonify({'error': 'Friend not found'}), 400


@app.route('/get_telegram_stars', methods=['POST'])
def get_telegram_stars():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    stars = user_stars.get(user_id, 0)
    return jsonify({'telegram_stars': stars})


# Новые эндпоинты для обработки приглашений друзей
@app.route('/send_friend_invite', methods=['POST'])
def send_friend_invite():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
    # Генерируем уникальный ID приглашения
    invite_id = f"friend_{user_id}_{int(time())}"
    
    # Сохраняем приглашение
    if user_id not in pending_friend_requests:
        pending_friend_requests[user_id] = []
    pending_friend_requests[user_id].append(invite_id)
    
    # Создаем ссылку на бот с параметром для обработки
    invite_link = f"https://t.me/{BOT_USERNAME}?start={invite_id}"
    
    save_data()
    
    return jsonify({
        'invite_link': invite_link,
        'message': "Want to be my friend in Affinia? 🌟"
    })


@app.route('/handle_friend_invite', methods=['POST'])
def handle_friend_invite():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    invitee_id = validated_user['user_id']
    
    if 'invite_id' not in data or 'decision' not in data:
        return jsonify({'error': 'Missing required fields'}), 400
    
    invite_id = data['invite_id']
    decision = data['decision']
    ensure_user_record(invitee_id)
    
    # Извлекаем ID приглашающего из invite_id
    if not invite_id.startswith('friend_'):
        return jsonify({'error': 'Invalid invite ID'}), 400
    
    parts = invite_id.split('_')
    if len(parts) < 3:
        return jsonify({'error': 'Invalid invite ID format'}), 400
    
    inviter_id = parts[1]
    ensure_user_record(inviter_id)
    
    if decision == 'accept':
        # Добавляем друзей друг к другу
        if inviter_id not in referrals:
            referrals[inviter_id] = []
        if invitee_id not in referrals:
            referrals[invitee_id] = []
        
        if invitee_id not in referrals[inviter_id]:
            referrals[inviter_id].append(invitee_id)
            new_friends_count[inviter_id] = new_friends_count.get(inviter_id, 0) + 1
        
        if inviter_id not in referrals[invitee_id]:
            referrals[invitee_id].append(inviter_id)
        
        # Начисляем AFN токены
        user_tokens[inviter_id] = user_tokens.get(inviter_id, 0) + 50  # 50 AFN приглашающему
        user_tokens[invitee_id] = user_tokens.get(invitee_id, 0) + 25  # 25 AFN принявшему
        
        # Добавляем уведомление для приглашающего
        if inviter_id not in user_notifications:
            user_notifications[inviter_id] = []
        user_notifications[inviter_id].append({
            'type': 'friend_accepted',
            'from_user': invitee_id,
            'timestamp': int(time()),
            'message': f'User accepted your friend request! 🎉'
        })
        
        save_data()
        
        return jsonify({
            'success': True,
            'message': 'Friend request accepted!',
            'tokens_earned': 25
        })
    
    elif decision == 'decline':
        # Добавляем уведомление для приглашающего о отклонении
        if inviter_id not in user_notifications:
            user_notifications[inviter_id] = []
        user_notifications[inviter_id].append({
            'type': 'friend_declined',
            'from_user': invitee_id,
            'timestamp': int(time()),
            'message': f'User declined your friend request 😔'
        })
        
        save_data()
        
        return jsonify({
            'success': True,
            'message': 'Friend request declined'
        })
    
    return jsonify({'error': 'Invalid decision'}), 400


@app.route('/send_consent_invite', methods=['POST'])
def send_consent_invite():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
    # Генерируем уникальный ID приглашения
    invite_id = f"consent_{user_id}_{int(time())}"
    
    # Сохраняем приглашение
    if user_id not in pending_consent_requests:
        pending_consent_requests[user_id] = []
    pending_consent_requests[user_id].append(invite_id)
    
    # Создаем ссылку на бот с параметром для обработки
    invite_link = f"https://t.me/{BOT_USERNAME}?start={invite_id}"
    
    save_data()
    
    return jsonify({
        'invite_link': invite_link,
        'message': "Ready to explore an intimate connection? 💕"
    })


@app.route('/handle_consent_invite', methods=['POST'])
def handle_consent_invite():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    partner_id = validated_user['user_id']
    
    if 'invite_id' not in data or 'decision' not in data:
        return jsonify({'error': 'Missing required fields'}), 400
    
    invite_id = data['invite_id']
    decision = data['decision']
    ensure_user_record(partner_id)
    
    # Извлекаем ID приглашающего из invite_id
    if not invite_id.startswith('consent_'):
        return jsonify({'error': 'Invalid invite ID'}), 400
    
    parts = invite_id.split('_')
    if len(parts) < 3:
        return jsonify({'error': 'Invalid invite ID format'}), 400
    
    inviter_id = parts[1]
    ensure_user_record(inviter_id)
    
    if decision == 'accept':
        # Добавляем партнеров друг к другу
        if inviter_id not in beloveds:
            beloveds[inviter_id] = []
        if partner_id not in beloveds:
            beloveds[partner_id] = []
        
        if partner_id not in beloveds[inviter_id]:
            beloveds[inviter_id].append(partner_id)
            new_partners_count[inviter_id] = new_partners_count.get(inviter_id, 0) + 1
        
        if inviter_id not in beloveds[partner_id]:
            beloveds[partner_id].append(inviter_id)
        
        # Начисляем AFN токены обоим
        user_tokens[inviter_id] = user_tokens.get(inviter_id, 0) + 100  # 100 AFN обоим
        user_tokens[partner_id] = user_tokens.get(partner_id, 0) + 100
        
        # Добавляем в историю consent
        timestamp = int(time())
        if inviter_id not in consent_history:
            consent_history[inviter_id] = []
        if partner_id not in consent_history:
            consent_history[partner_id] = []
        
        consent_record = {
            'partner_id': partner_id,
            'timestamp': timestamp,
            'status': 'active',
            'type': 'intimate'
        }
        consent_history[inviter_id].append(consent_record)
        
        consent_record_partner = {
            'partner_id': inviter_id,
            'timestamp': timestamp,
            'status': 'active',
            'type': 'intimate'
        }
        consent_history[partner_id].append(consent_record_partner)
        
        # Добавляем уведомление для приглашающего
        if inviter_id not in user_notifications:
            user_notifications[inviter_id] = []
        user_notifications[inviter_id].append({
            'type': 'consent_accepted',
            'from_user': partner_id,
            'timestamp': timestamp,
            'message': f'Your intimate connection request was accepted! 💕'
        })
        
        save_data()
        
        return jsonify({
            'success': True,
            'message': 'Consent request accepted!',
            'tokens_earned': 100
        })
    
    elif decision == 'decline':
        # Добавляем уведомление для приглашающего о отклонении
        if inviter_id not in user_notifications:
            user_notifications[inviter_id] = []
        user_notifications[inviter_id].append({
            'type': 'consent_declined',
            'from_user': partner_id,
            'timestamp': int(time()),
            'message': f'Your intimate connection request was declined 💔'
        })
        
        save_data()
        
        return jsonify({
            'success': True,
            'message': 'Consent request declined'
        })
    
    return jsonify({'error': 'Invalid decision'}), 400


@app.route('/get_notifications', methods=['POST'])
def get_notifications():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    notifications = user_notifications.get(user_id, [])
    
    # Очищаем уведомления после отправки
    if user_id in user_notifications:
        user_notifications[user_id] = []
        save_data()
    
    return jsonify({'notifications': notifications})


@app.route('/get_new_friends_count', methods=['POST'])
def get_new_friends_count():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    count = new_friends_count.get(user_id, 0)
    
    return jsonify({'new_friends_count': count})


@app.route('/clear_new_friends', methods=['POST'])
def clear_new_friends():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    if user_id in new_friends_count:
        new_friends_count[user_id] = 0
        save_data()
    
    return jsonify({'success': True})


@app.route('/get_consent_history', methods=['POST'])
def get_consent_history_endpoint():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    history = consent_history.get(user_id, [])
    
    # Подсчитываем статистику
    total_consents = len(history)
    active_consents = len([h for h in history if h.get('status') == 'active'])
    this_month_consents = len([h for h in history if h.get('timestamp', 0) > time() - 2592000])  # 30 дней
    
    return jsonify({
        'consent_history': history,
        'total_consents': total_consents,
        'active_consents': active_consents,
        'this_month_consents': this_month_consents
    })


# =====  WEBHOOK ПОДДЕРЖКА (готова, но не активна) =====
@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint для будущего использования"""
    if not USE_WEBHOOK:
        return jsonify({'error': 'Webhook not enabled'}), 400
    
    try:
        json_data = request.get_json()
        update = Update.de_json(json_data, bot)
        
        # Обрабатываем update в отдельной задаче
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(application.process_update(update))
        loop.close()
        
        return jsonify({'ok': True})
    except Exception as e:
        logging.error(f'❌ Webhook error: {e}')
        return jsonify({'error': str(e)}), 500


# Функция для настройки webhook
async def setup_webhook():
    """Настройка webhook (для будущего использования)"""
    webhook_url = f"{WEBAPP_URL}/webhook"
    try:
        await bot.set_webhook(webhook_url)
        logging.info(f'✅ Webhook set to: {webhook_url}')
    except Exception as e:
        logging.error(f'❌ Failed to set webhook: {e}')
        raise


# Функция для удаления webhook
async def remove_webhook():
    """Удаление webhook"""
    try:
        await bot.delete_webhook()
        logging.info('✅ Webhook removed')
    except Exception as e:
        logging.error(f'❌ Failed to remove webhook: {e}')


if __name__ == '__main__':
    load_data()
    # load_beloveds()

    logging.info(f'🚀 Starting Affinia Bot (USE_WEBHOOK: {USE_WEBHOOK})')
    
    if USE_WEBHOOK:
        # WEBHOOK MODE: Flask и бот в одном процессе
        logging.info('🌐 Using WEBHOOK mode')
        
        # Настраиваем webhook асинхронно
        async def init_webhook():
            await run_bot_webhook()
        
        # Запускаем webhook setup
        import asyncio
        webhook_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(webhook_loop)
        webhook_loop.run_until_complete(init_webhook())
        webhook_loop.close()
        
        # Flask запускается синхронно (будет принимать webhook calls)
        logging.info('🌐 Starting Flask server for webhook...')
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
        
    else:
        # POLLING MODE: Flask в отдельном потоке, бот в основном
        logging.info('🔄 Using POLLING mode (current stable setup)')
        
        # Flask в отдельном потоке
        flask_thread = threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False),
            daemon=True
        )
        flask_thread.start()
        
        # Запускаем бота в основном потоке синхронно
        try:
            run_bot_polling()
        except KeyboardInterrupt:
            print("Bot stopped by user (Ctrl+C)")
            # Если надо - дожидаемся Flask
            flask_thread.join()
