import os
import sys
import json
import logging
import threading
import asyncio
import qrcode
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
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

# PostgreSQL is the only storage (see services/database_service.py)
from services.database_service import DatabaseService

# Настройка логирования с безопасностью
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # stdout → Heroku logs; файловая система дайно эфемерна
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

# За Heroku-роутером реальный IP клиента — последний элемент X-Forwarded-For (роутер дописывает его сам,
# всё что левее — прислал клиент и может быть подделано). PROXY_HOPS=0 для запуска без прокси.
PROXY_HOPS = int(os.getenv('PROXY_HOPS', '1' if os.getenv('DYNO') else '0'))
if PROXY_HOPS > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS, x_proto=1, x_host=1)


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, скользящее окно; достаточно для одного дино)
# ---------------------------------------------------------------------------
from collections import defaultdict
from time import time

class RateLimited(Exception):
    """Поднимается из любого места обработчика; превращается в 429 через errorhandler."""
    def __init__(self, message='Too many requests. Please try again later.'):
        super().__init__(message)
        self.message = message


_rate_buckets = defaultdict(list)
_rate_lock = threading.Lock()
_rate_calls = 0

# Общие лимиты. Фронтенд опрашивает /get_notifications каждые 15 с (240/ч), поэтому per-user не ниже ~1000/ч.
# Лимит по IP щадящий: мобильные операторы сажают тысячи людей за один NAT-адрес.
USER_RATE_LIMIT = (1200, 3600)
IP_RATE_LIMIT = (3000, 3600)
AUTH_FAIL_LIMIT = (30, 600)        # неудачных проверок initData с одного IP за 10 минут


def enforce_rate_limit(key: str, limit: int, window: int, message: str = None) -> None:
    """Регистрирует обращение по ключу; при превышении лимита поднимает RateLimited."""
    global _rate_calls
    now = time()
    with _rate_lock:
        bucket = _rate_buckets[key]
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            logging.warning(f'🛡️ rate limit exceeded: {key}')
            raise RateLimited(message or RateLimited().message)
        bucket.append(now)
        # Периодически выбрасываем протухшие ключи, чтобы словарь не рос бесконечно
        _rate_calls += 1
        if _rate_calls % 2000 == 0:
            stale = [k for k, b in _rate_buckets.items() if not b or now - b[-1] > 3600]
            for k in stale:
                del _rate_buckets[k]


@app.errorhandler(RateLimited)
def _handle_rate_limited(e):
    return jsonify({'error': e.message}), 429


@app.before_request
def _ip_rate_limit():
    if request.method == 'POST' and request.remote_addr:
        enforce_rate_limit(f'ip:{request.remote_addr}', *IP_RATE_LIMIT)


# Максимальный возраст initData. Telegram не инвалидирует подпись сам: без этой проверки
# однажды перехваченная строка работала бы вечно.
INIT_DATA_MAX_AGE = int(os.getenv('INIT_DATA_MAX_AGE', str(24 * 3600)))
CLOCK_SKEW = 300


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

        # Свежесть: auth_date подписан Telegram, подделать его нельзя
        auth_date = parsed.get('auth_date', '')
        if not auth_date.isdigit():
            logging.warning('🛡️ initData validation failed: no auth_date')
            return None
        age = time() - int(auth_date)
        if age > INIT_DATA_MAX_AGE or age < -CLOCK_SKEW:
            logging.warning(f'🛡️ initData validation failed: stale auth_date (age {int(age)}s)')
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
    
    user_data = validate_init_data(init_data, API_TOKEN)
    
    if user_data and user_data.get('user_id'):
        enforce_rate_limit(f"user:{user_data['user_id']}", *USER_RATE_LIMIT)
        return user_data

    # Неудачные попытки считаем отдельно и строже: перебор подписей с одного IP быстро упирается в 429
    if request.remote_addr:
        enforce_rate_limit(f'authfail:{request.remote_addr}', *AUTH_FAIL_LIMIT)
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
    """Гарантирует, что пользователь есть в PostgreSQL (и в кэше), даже с нулевым балансом."""
    if not user_id or not str(user_id).isdigit():
        return False

    try:
        user, created = DatabaseService.ensure_user(user_id)
        balance = user.balance or 0
    except Exception as e:
        logging.error(f"Database error in ensure_user_record: {e}")
        return False

    with data_lock:
        user_tokens.setdefault(user_id, balance)

    if created:
        display_name = get_user_display_name(user_id)
        logging.info(f'🆕 NEW USER REGISTERED: {display_name} with 0 AFN')

        # Уведомление админу о новом пользователе
        if ADMIN_ID and user_id != ADMIN_ID:
            msg = f"🆕 <b>NEW USER REGISTERED!</b>\n\n👤 <b>User:</b> {display_name}\n🆔 <b>ID:</b> <code>{user_id}</code>"
            try:
                run_bot_coro(application.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='HTML'), wait=False)
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

    with data_lock:
        unchanged = user_profiles.get(user_id) == profile
        user_profiles[user_id] = profile
    if unchanged:
        return

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

async def _remember_bot_loop(_application) -> None:
    """post_init: запоминаем loop, в котором реально крутится бот (не полагаемся на то, какой loop выберет PTB)."""
    global bot_loop
    bot_loop = asyncio.get_running_loop()


# Инициализация бота с проверкой токена
try:
    bot = Bot(token=API_TOKEN)
    application = (Application.builder().token(API_TOKEN).connection_pool_size(50)
                   .post_init(_remember_bot_loop).build())
    logging.info(f'✅ Bot initialized successfully with username: @{BOT_USERNAME}')
except Exception as e:
    logging.error(f'❌ Failed to initialize bot: {e}')
    sys.exit(1)

URL = WEBAPP_URL


def run_bot_coro(coro, timeout: float = 10, wait: bool = True):
    """Выполняет корутину Telegram Bot API в event loop бота.

    Из потока Flask — через run_coroutine_threadsafe; изнутри самого loop бота —
    планирует задачу (блокироваться там нельзя, иначе deadlock).
    """
    loop = bot_loop
    if loop is None or not loop.is_running():
        coro.close()
        raise RuntimeError('Bot event loop is not running')
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        return loop.create_task(coro)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout) if wait else future


def init_database() -> None:
    """Connect to PostgreSQL. The database is the only storage: fail fast if it is unavailable."""
    db_url = os.getenv('HEROKU_POSTGRESQL_COBALT_URL') or os.getenv('DATABASE_URL')
    if not db_url:
        logging.error('❌ CRITICAL: no HEROKU_POSTGRESQL_COBALT_URL / DATABASE_URL set')
        sys.exit(1)
    if not DatabaseService.initialize(db_url):
        logging.error('❌ CRITICAL: database connection failed')
        sys.exit(1)
    logging.info('✅ PostgreSQL database initialized')


def load_data():
    """Rebuild the in-memory read cache from PostgreSQL."""
    global user_tokens, referrals, beloveds, user_stars, user_profiles
    global pending_friend_requests, pending_consent_requests
    global user_notifications, new_friends_count, new_partners_count, consent_history

    init_database()
    snap = DatabaseService.load_snapshot()
    with data_lock:
        user_tokens = snap['user_tokens']
        referrals = snap['referrals']
        beloveds = snap['beloveds']
        user_stars = snap['user_stars']
        user_profiles = snap['user_profiles']
        pending_friend_requests = snap['pending_friend_requests']
        pending_consent_requests = snap['pending_consent_requests']
        user_notifications = snap['user_notifications']
        new_friends_count = snap['new_friends_count']
        new_partners_count = snap['new_partners_count']
        consent_history = snap['consent_history']
    logging.info(
        f"✅ Loaded from PostgreSQL: {len(user_tokens)} users, "
        f"{sum(len(v) for v in referrals.values()) // 2} friend links, "
        f"{sum(len(v) for v in beloveds.values()) // 2} partner links, "
        f"{sum(len(v) for v in consent_history.values()) // 2} consents"
    )


def _link_both(cache: dict, a: str, b: str) -> None:
    """Mirror a bidirectional link into the read cache."""
    with data_lock:
        cache.setdefault(a, [])
        cache.setdefault(b, [])
        if b not in cache[a]:
            cache[a].append(b)
        if a not in cache[b]:
            cache[b].append(a)


def _unlink_both(cache: dict, a: str, b: str) -> None:
    with data_lock:
        if b in cache.get(a, []):
            cache[a].remove(b)
        if a in cache.get(b, []):
            cache[b].remove(a)


def credit_afn(user_id: str, delta: int, reason: str, ref: str = None) -> int:
    """Change AFN balance in the DB (atomic) and mirror into the cache. Returns new balance."""
    new_balance = DatabaseService.add_balance(user_id, delta, reason, ref)
    with data_lock:
        user_tokens[user_id] = new_balance
    return new_balance


def notify(user_id: str, type_: str, from_user: str, message: str) -> None:
    record = DatabaseService.add_notification(user_id, type_, from_user, message)
    with data_lock:
        user_notifications.setdefault(user_id, []).append(record)


# ===== Invites =====
# Токен выдаёт только сервер; принять можно лишь существующий, неиспользованный и непросроченный.
INVITE_TTL = {'friend': timedelta(days=30), 'consent': timedelta(days=7)}


def issue_invite(user_id: str, kind: str) -> tuple[str, str]:
    """Создаёт инвайт в БД. Возвращает (token, deep link). Префикс kind_ нужен боту и фронту для диспетчеризации."""
    # Ограничивает и спам ссылками, и рост таблицы invites
    enforce_rate_limit(f"invite_issue:{user_id}", 30, 3600, 'Too many invitations created. Please try again later.')
    token = f"{kind}_{secrets.token_urlsafe(16)}"
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + INVITE_TTL[kind]
    if not DatabaseService.create_invite(token, user_id, kind, expires_at):
        raise RuntimeError('could not create invite')
    cache = pending_friend_requests if kind == 'friend' else pending_consent_requests
    with data_lock:
        cache.setdefault(user_id, []).append(token)
    return token, f"https://t.me/{BOT_USERNAME}?start={token}"


def lookup_invite(token: str, kind: str, acceptor_id: str = None):
    """Возвращает (invite, None) если токен валиден, иначе (None, причина)."""
    if acceptor_id is not None:
        enforce_rate_limit(f"invite_lookup:{acceptor_id}", 60, 3600)
    if not isinstance(token, str) or not token.startswith(f"{kind}_") or len(token) > 120:
        return None, 'Invalid invite ID'
    invite = DatabaseService.get_invite(token)
    if invite is None or invite.kind != kind:
        return None, 'Invite not found'
    if invite.used_at is not None:
        return None, 'Invite already used'
    if invite.expires_at is not None and invite.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return None, 'Invite expired'
    if acceptor_id is not None and invite.inviter_id == acceptor_id:
        return None, 'You cannot accept your own invite'
    return invite, None


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
    
    validated_user = get_validated_user(data)
    if not validated_user:
        logging.warning(f'🛡️ Unauthorized /get_balance attempt from IP: {request.remote_addr}')
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
    
    # Создаём запись пользователя и сохраняем профиль (в БД пишется только при изменении)
    ensure_user_record(user_id)
    with data_lock:
        changed = user_profiles.get(user_id) != profile
        user_profiles[user_id] = profile
    if changed:
        logging.info(f'👤 Profile from Mini App: {user_id} → @{profile["username"] or "unknown"}')
        try:
            DatabaseService.update_profile(user_id, **profile)
        except Exception as e:
            logging.error(f"DB error saving profile: {e}")
    
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    
    display_name = get_user_display_name(user_id)
    logging.info(f'💰 Balance check: {display_name} → {balance} AFN')
    return jsonify({'balance': balance})


@app.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
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
    return jsonify({'referral_link': referral_link, 'qr_code': _qr_png_base64(referral_link)})


def _qr_png_base64(payload: str) -> str:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return b64encode(buffered.getvalue()).decode('utf-8')


@app.route('/create_consent_link', methods=['POST'])
def create_consent_link():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    _, consent_link = issue_invite(user_id, 'consent')
    return jsonify({'link': consent_link})


@app.route('/generate_consent_qr', methods=['POST'])
def generate_consent_qr():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    _, consent_link = issue_invite(user_id, 'consent')
    return jsonify({'consent_link': consent_link, 'qr_code': _qr_png_base64(consent_link)})


@app.route('/invite_info', methods=['POST'])
def invite_info():
    """Кто прислал приглашение — для модального окна у получателя."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    token = data.get('invite_id', '')
    kind = 'friend' if isinstance(token, str) and token.startswith('friend_') else 'consent'
    invite, error = lookup_invite(token, kind, validated_user['user_id'])
    if error:
        return jsonify({'error': error}), 400

    inviter_id = invite.inviter_id
    profile = user_profiles.get(inviter_id, {})
    if profile.get('username'):
        display_name = f"@{profile['username']}"
    else:
        display_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or f"User {inviter_id[:6]}..."
    avatar = get_avatar_data_url(inviter_id) or f'https://api.dicebear.com/6.x/avataaars/svg?seed={inviter_id}'

    return jsonify({'kind': invite.kind, 'inviter_id': inviter_id, 'display_name': display_name, 'avatar': avatar})


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
    
    beloved_data = []
    with data_lock:
        partner_ids = list(beloveds.get(user_id, []))
        history = list(consent_history.get(user_id, []))
    for b_id in partner_ids:
        # Статус и дата — из реальных записей consents
        pair = [c for c in history if c.get('partner_id') == b_id]
        active = [c for c in pair if c.get('status') == 'active']
        relationship_status = 'current' if active else ('past' if pair else 'potential')
        added_ts = min((c.get('timestamp', 0) for c in pair), default=0)

        beloved_data.append({
            "user_id": b_id,
            "relationship_status": relationship_status,
            "trust_level": "partner",
            "added_date": added_ts * 1000 if added_ts else None,  # мс для фронтенда
            "consent_type": "intimate",
            "active_consent_id": active[-1].get('id') if active else None,
            "last_consent_at": max((c.get('timestamp', 0) for c in pair), default=None),
        })
    
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

    if beloved_id in beloveds.get(user_id, []):
        DatabaseService.remove_partner(user_id, beloved_id)
        _unlink_both(beloveds, user_id, beloved_id)
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
    
    enforce_rate_limit(f"donate:{user_id}", 10, 3600, 'Too many donation requests. Please wait before trying again.')
    
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

    try:
        invoice_link = run_bot_coro(application.bot.create_invoice_link(
            title=f"Support Affinia - {tier}",
            description=f"Thank you for supporting our app with {amount} Telegram Stars! 💖",
            payload=f"donation_{user_id}_{amount}",
            provider_token="",  # Для Telegram Stars оставляем пустым
            currency="XTR",     # Валюта Telegram Stars
            prices=[LabeledPrice(label=f"{tier} ({amount} Stars)", amount=amount)]
        ))
        logging.info(f'Donation invoice created for user {user_id}: {amount} stars, tier: {tier}')
        return jsonify({'success': True, 'invoice_link': invoice_link})
    except Exception as e:
        logging.error(f"Error creating invoice for user {user_id}: {e}")
        return jsonify({'error': 'Failed to create donation invoice'}), 500

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
            await handle_invitation(update, context, arg, 'friend')
        elif arg.startswith("consent_"):
            await handle_invitation(update, context, arg, 'consent')
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
        # add_friend возвращает True только если связи ещё не было — награда начисляется один раз
        if DatabaseService.add_friend(referrer_id, new_user_id):
            _link_both(referrals, referrer_id, new_user_id)
            ref = f'referral:{referrer_id}:{new_user_id}'
            credit_afn(referrer_id, 10, 'referral_inviter', ref)
            credit_afn(new_user_id, 5, 'referral_joined', ref)

            logging.info(f'🎉 REFERRAL SUCCESS! {new_user_id} invited by {referrer_id} | +10 / +5 AFN')

            referrer_text = 'You have been credited with 10 AffinyCoin for inviting a new user.'
            new_user_text = ('Thank you for registering! You have been credited with 5 AffinyCoin.\n'
                             'Your referrer has been credited with 10 AffinyCoin.')
            await update.message.reply_text(new_user_text)
            await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
        else:
            error_text = "You have already registered using a referral link."
            await update.message.reply_text(error_text)

    await show_menu(update, context, new_user_id)


INVITE_TEXT = {
    'friend': ("🌟 You've been invited to become friends in Affinia!\n\n"
               "Click the button below to open the app and respond to the invitation.", "🚀 Open Affinia"),
    'consent': ("💕 You've been invited to establish an intimate connection in Affinia!\n\n"
                "This is a special invitation to become partners. "
                "Click the button below to open the app and respond.", "💕 Open Affinia"),
}


async def handle_invitation(update: Update, context: CallbackContext, token: str, kind: str) -> None:
    """Deep link с инвайтом: проверяем токен в БД и открываем мини-апп с ним."""
    user = update.message.from_user
    user_id = str(user.id)
    update_user_profile(user)
    ensure_user_record(user_id)

    try:
        invite, error = lookup_invite(token, kind, user_id)
        reply = "This invitation link is invalid, expired or already used. Please ask for a new one."
    except RateLimited as e:
        invite, error, reply = None, e.message, e.message
    if error:
        logging.info(f'Invite rejected for {user_id}: {error} ({token[:16]}...)')
        await update.message.reply_text(reply)
        await show_menu(update, context, user_id)
        return

    text, button = INVITE_TEXT[kind]
    open_app_url = f"{URL}/mini_app?invite={token}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button, web_app=WebAppInfo(url=open_app_url))]])
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
    else:
        logging.warning(f'Unknown callback from {user_id}: {command[:40]}')


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
    try:
        _, consent_link = issue_invite(user_id, 'consent')
        text = f'Share this link to request consent: {consent_link}'
    except RateLimited as e:
        text = e.message
    await update.edit_message_text(text)
    await show_menu(update, context, user_id)


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
    
    # Сумма и id платежа — из самого платежа, а не из payload (payload формирует наш же код, ему доверять нельзя)
    payment = update.message.successful_payment
    amount = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    try:
        new_total = DatabaseService.record_payment(
            charge_id, user_id, amount, payment.currency, payment.invoice_payload
        )
    except Exception as e:
        logging.error(f'Failed to record payment {charge_id}: {e}')
        new_total = None

    if new_total is None:
        logging.warning(f'Duplicate or failed payment {charge_id} for {user_id}, not crediting again')
    else:
        with data_lock:
            user_stars[user_id] = new_total
        logging.info(f'⭐ PAYMENT: {user_id} +{amount} {payment.currency} (charge {charge_id})')

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

    if friend_id in referrals.get(user_id, []):
        DatabaseService.remove_friend(user_id, friend_id)
        _unlink_both(referrals, user_id, friend_id)
        return jsonify({'success': True})

    return jsonify({'error': 'Friend not found'}), 400


# Кэш аватарок: user_id -> (expires_at, data_url | None). Токен бота на клиент не уходит.
_avatar_cache: dict = {}
AVATAR_CACHE_TTL = 3600
AVATAR_CACHE_MAX = 2000


async def _fetch_avatar_data_url(user_id: str):
    photos = await application.bot.get_user_profile_photos(int(user_id), limit=1)
    if not photos or not photos.photos:
        return None
    smallest = photos.photos[0][0]
    tg_file = await application.bot.get_file(smallest.file_id)
    raw = await tg_file.download_as_bytearray()
    return 'data:image/jpeg;base64,' + b64encode(bytes(raw)).decode('ascii')


def get_avatar_data_url(user_id: str):
    now = time()
    cached = _avatar_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        data_url = run_bot_coro(_fetch_avatar_data_url(user_id))
    except Exception as e:
        logging.warning(f'Avatar fetch failed for {user_id}: {e}')
        data_url = None
    if len(_avatar_cache) >= AVATAR_CACHE_MAX:
        _avatar_cache.clear()
    _avatar_cache[user_id] = (now + AVATAR_CACHE_TTL, data_url)
    return data_url


def is_connected(user_id: str, target_id: str) -> bool:
    """Связаны ли пользователи (друзья или партнёры)."""
    if user_id == target_id:
        return True
    return target_id in referrals.get(user_id, []) or target_id in beloveds.get(user_id, [])


@app.route('/user_card', methods=['POST'])
def user_card():
    """Имя и аватар другого пользователя — только для связанных с запрашивающим."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    target_id = str(data.get('target_id', '')).strip()
    if not target_id.isdigit():
        return jsonify({'error': 'target_id is required'}), 400

    if not is_connected(user_id, target_id):
        return jsonify({'error': 'Not allowed'}), 403

    profile = user_profiles.get(target_id, {})
    username = profile.get('username', '')
    first_name = profile.get('first_name', '')
    last_name = profile.get('last_name', '')
    if username:
        display_name = f'@{username}'
    elif first_name or last_name:
        display_name = f'{first_name} {last_name}'.strip()
    else:
        display_name = f'User {target_id[:6]}...'

    avatar = get_avatar_data_url(target_id) or f'https://api.dicebear.com/6.x/avataaars/svg?seed={target_id}'

    return jsonify({'user_id': target_id, 'display_name': display_name, 'avatar': avatar})


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
    
    _, invite_link = issue_invite(user_id, 'friend')

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

    # Инвайтер берётся из записи в БД, а не из строки токена
    invite, error = lookup_invite(invite_id, 'friend', invitee_id)
    if error:
        return jsonify({'error': error}), 400
    inviter_id = invite.inviter_id

    if decision == 'accept':
        tokens_earned = 0
        # add_friend возвращает True только для новой связи — награда и уведомление один раз
        if DatabaseService.add_friend(inviter_id, invitee_id):
            _link_both(referrals, inviter_id, invitee_id)
            credit_afn(inviter_id, 50, 'friend_invite_inviter', invite_id)
            credit_afn(invitee_id, 25, 'friend_invite_accepted', invite_id)
            tokens_earned = 25
            DatabaseService.bump_unseen(inviter_id, friends=1)
            with data_lock:
                new_friends_count[inviter_id] = new_friends_count.get(inviter_id, 0) + 1
            notify(inviter_id, 'friend_accepted', invitee_id, 'User accepted your friend request! 🎉')
        DatabaseService.mark_invite_used(invite_id, invitee_id, 'accept')

        return jsonify({
            'success': True,
            'message': 'Friend request accepted!',
            'tokens_earned': tokens_earned
        })

    elif decision == 'decline':
        notify(inviter_id, 'friend_declined', invitee_id, 'User declined your friend request 😔')
        DatabaseService.mark_invite_used(invite_id, invitee_id, 'decline')

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
    
    _, invite_link = issue_invite(user_id, 'consent')

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

    # Инвайтер берётся из записи в БД, а не из строки токена
    invite, error = lookup_invite(invite_id, 'consent', partner_id)
    if error:
        return jsonify({'error': error}), 400
    inviter_id = invite.inviter_id

    if decision == 'accept':
        tokens_earned = 0
        # add_partner возвращает True только для новой связи — награда, consent и уведомление один раз
        if DatabaseService.add_partner(inviter_id, partner_id):
            _link_both(beloveds, inviter_id, partner_id)
            credit_afn(inviter_id, 100, 'consent_inviter', invite_id)
            credit_afn(partner_id, 100, 'consent_accepted', invite_id)
            tokens_earned = 100

            consent = DatabaseService.add_consent(inviter_id, partner_id, 'intimate')
            with data_lock:
                consent_history.setdefault(inviter_id, []).append({**consent, 'partner_id': partner_id})
                consent_history.setdefault(partner_id, []).append({**consent, 'partner_id': inviter_id})

            DatabaseService.bump_unseen(inviter_id, partners=1)
            with data_lock:
                new_partners_count[inviter_id] = new_partners_count.get(inviter_id, 0) + 1
            notify(inviter_id, 'consent_accepted', partner_id, 'Your intimate connection request was accepted! 💕')
            logging.info(f'❤️ CONSENT ACCEPTED: {partner_id} ← {inviter_id}')
        DatabaseService.mark_invite_used(invite_id, partner_id, 'accept')

        return jsonify({
            'success': True,
            'message': 'Consent request accepted!',
            'tokens_earned': tokens_earned
        })

    elif decision == 'decline':
        notify(inviter_id, 'consent_declined', partner_id, 'Your intimate connection request was declined 💔')
        DatabaseService.mark_invite_used(invite_id, partner_id, 'decline')

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
    with data_lock:
        notifications = user_notifications.pop(user_id, [])

    # Доставлено один раз: в БД помечаем, в кэше убираем
    if notifications:
        DatabaseService.mark_notifications_delivered(user_id)

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
    if new_friends_count.get(user_id):
        DatabaseService.clear_unseen(user_id, friends=True)
        with data_lock:
            new_friends_count[user_id] = 0

    return jsonify({'success': True})


@app.route('/revoke_consent', methods=['POST'])
def revoke_consent():
    """Отзыв согласия одной из сторон. Запись не удаляется — меняется статус."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    try:
        consent_id = int(data.get('consent_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'consent_id is required'}), 400

    if not DatabaseService.revoke_consent(consent_id, user_id):
        return jsonify({'error': 'Consent not found'}), 404

    revoked_ts = int(time())
    other_id = None
    with data_lock:
        for record in consent_history.get(user_id, []):
            if record.get('id') == consent_id:
                record['status'] = 'revoked'
                record['revoked_at'] = revoked_ts
                other_id = record.get('partner_id')
        if other_id:
            for record in consent_history.get(other_id, []):
                if record.get('id') == consent_id:
                    record['status'] = 'revoked'
                    record['revoked_at'] = revoked_ts

    if other_id:
        notify(other_id, 'consent_revoked', user_id, 'A consent with you has been withdrawn.')
        logging.info(f'🚫 CONSENT REVOKED: id={consent_id} by {user_id}')

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
