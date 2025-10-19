import os
import sys
import json
import logging
import threading
import asyncio
import qrcode
from io import BytesIO
from base64 import b64encode
from flask import Flask, render_template, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot, LabeledPrice
from telegram.ext import (Application, CommandHandler, CallbackContext, CallbackQueryHandler, PreCheckoutQueryHandler, MessageHandler, filters)
from concurrent.futures import TimeoutError
bot_loop = None

from json.decoder import JSONDecodeError

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

user_tokens = {}
referrals = {}
beloveds = {}
user_stars = {}
pending_friend_requests = {}  # Хранение ожидающих запросов дружбы
pending_consent_requests = {}  # Хранение ожидающих запросов партнерства
user_notifications = {}  # Хранение уведомлений для пользователей
new_friends_count = {}  # Счетчик новых непросмотренных друзей
new_partners_count = {}  # Счетчик новых непросмотренных партнеров
consent_history = {}  # История всех consent связей
data_lock = threading.Lock()

# Безопасная загрузка конфигурации
API_TOKEN = os.getenv('API_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME')
WEBAPP_URL = os.getenv('WEBAPP_URL')
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
    WEBAPP_URL = "https://affinia-f56170a22b09.herokuapp.com"

# Инициализация бота с проверкой токена
try:
    bot = Bot(token=API_TOKEN)
    application = Application.builder().token(API_TOKEN).connection_pool_size(50).build()
    logging.info(f'✅ Bot initialized successfully with username: @{BOT_USERNAME}')
except Exception as e:
    logging.error(f'❌ Failed to initialize bot: {e}')
    sys.exit(1)

URL = WEBAPP_URL


def load_data():
    global user_tokens, referrals, beloveds, user_stars, pending_friend_requests, pending_consent_requests
    global user_notifications, new_friends_count, new_partners_count, consent_history
    try:
        with open('user_tokens.json', 'r') as f:
            data = json.load(f)
            user_tokens = data.get('user_tokens', {})
            referrals = data.get('referrals', {})
            beloveds = data.get('beloveds', {})
            user_stars = data.get('user_stars', {})
            pending_friend_requests = data.get('pending_friend_requests', {})
            pending_consent_requests = data.get('pending_consent_requests', {})
            user_notifications = data.get('user_notifications', {})
            new_friends_count = data.get('new_friends_count', {})
            new_partners_count = data.get('new_partners_count', {})
            consent_history = data.get('consent_history', {})  # Новая строка
            print(f"Loaded data: {user_tokens}, {referrals}, {beloveds}, {user_stars}")
    except FileNotFoundError:
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}  # Новая строка
        print("Data file not found, starting with an empty dictionary")
    except JSONDecodeError:
        logging.error('Error decoding JSON from user_tokens.json. Starting with empty data.')
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}  # Новая строка
    except Exception as e:
        logging.error(f"Unexpected error while loading data: {e}")
        user_tokens = {}
        referrals = {}
        beloveds = {}
        user_stars = {}  # Новая строка

def save_data():
    try:
        with data_lock:
            with open('user_tokens.json', 'w') as f:
                json.dump({
                    'user_tokens': user_tokens,
                    'referrals': referrals,
                    'beloveds': beloveds,
                    'user_stars': user_stars,
                    'pending_friend_requests': pending_friend_requests,
                    'pending_consent_requests': pending_consent_requests,
                    'user_notifications': user_notifications,
                    'new_friends_count': new_friends_count,
                    'new_partners_count': new_partners_count,
                    'consent_history': consent_history
                }, f)
        print(f"Saved data: {user_tokens}, {referrals}, {beloveds}, {user_stars}")
    except Exception as e:
        logging.error(f"Unexpected error while saving data: {e}")


# Flask-маршруты
@app.route('/mini_app')
def mini_app():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')


@app.route('/static/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/json')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename, mimetype='application/json')

@app.route('/tonconnect-manifest.json')
def tonconnect_manifest():
    # TON Connect manifest for wallet integration
    return send_from_directory('static', 'tonconnect-manifest.json', mimetype='application/json')


@app.route('/get_balance', methods=['POST'])
def get_balance():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    
    # Rate limiting
    if not check_rate_limit(user_id):
        logging.warning(f'Rate limit exceeded for user {user_id}')
        return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429
    
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    
    logging.info(f'Balance requested for user {user_id}: {balance} AFN')
    return jsonify({'balance': balance})


@app.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    
    # Rate limiting
    if not check_rate_limit(user_id):
        logging.warning(f'Rate limit exceeded for user {user_id} (referral link)')
        return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429
    
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    logging.info(f'Referral link created for user {user_id}')
    return jsonify({'link': referral_link})


@app.route('/generate_referral_qr', methods=['POST'])
def generate_referral_qr():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
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


# Например, в эндпоинте /create_consent_link:
@app.route('/create_consent_link', methods=['POST'])
def create_consent_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    user_id = str(data['user_id'])
    # Формируем ссылку, которая ведёт в бот, как в случае с рефералом:
    consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
    return jsonify({'link': consent_link})


@app.route('/generate_consent_qr', methods=['POST'])
def generate_consent_qr():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
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
    data = request.json
    required_fields = ['referrer_id', 'new_user_id', 'decision']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing parameters'}), 400

    referrer_id = str(data['referrer_id'])
    new_user_id = str(data['new_user_id'])
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
        try:
            bot.send_message(chat_id=referrer_id, text='Your consent request has been declined.')
        except Exception as e:
            logging.error(f"Error sending message: {e}")
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Invalid decision'}), 400


@app.route('/get_beloved', methods=['POST'])
def get_beloved():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    user_id = str(data['user_id'])
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
    data = request.json
    if 'user_id' not in data or 'beloved_id' not in data:
        return jsonify({'error': 'user_id and beloved_id are required'}), 400

    user_id = str(data['user_id'])
    beloved_id = str(data['beloved_id'])

    if user_id in beloveds and beloved_id in beloveds[user_id]:
        beloveds[user_id].remove(beloved_id)
        if beloved_id in beloveds and user_id in beloveds[beloved_id]:
            beloveds[beloved_id].remove(user_id)
        save_data()
        return jsonify({'success': True})
    return jsonify({'error': 'Beloved not found'}), 400

@app.route('/get_donate_link', methods=['POST'])
def get_donate_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    
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
    user_id = str(update.message.from_user.id)
    if args:
        arg = args[0]
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
        else:
            # Обычная реферальная ссылка
            await referral_start(update, context, arg)
    else:
        await show_menu(update, context, user_id)


async def referral_start(update: Update, context: CallbackContext, referrer_id: str) -> None:
    new_user_id = str(update.message.from_user.id)

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
    new_user_id = str(update.message.from_user.id)
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
    user_id = str(update.message.from_user.id)
    inviter_id = invite_id.split('_')[1] if '_' in invite_id else ''
    
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
    user_id = str(update.message.from_user.id)
    inviter_id = invite_id.split('_')[1] if '_' in invite_id else ''
    
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
    if user_id is None:
        user_id = str(update.message.from_user.id if hasattr(update.message, 'from_user') else update.from_user.id)

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
    user_id = str(update.message.from_user.id if not callback_query else update.from_user.id)
    bal = user_tokens.get(user_id, 0)
    text = f'Your balance: {bal} AffinyCoin.'
    if callback_query:
        await update.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    await show_menu(update, context, user_id)


async def create_referral_link_btn(update: Update, context: CallbackContext) -> None:
    user_id = str(update.from_user.id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = f'Share this link to invite friends and earn tokens: {referral_link}'
    await update.edit_message_text(text)
    await show_menu(update, context, user_id)


async def create_consent_link_btn(update: Update, context: CallbackContext) -> None:
    user_id = str(update.from_user.id)
    # Больше не списываем токены за создание consent link
    consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
    text = f'Share this link to request consent: {consent_link}'
    await update.edit_message_text(text)
    await show_menu(update, context, user_id)


async def process_consent(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
    referrer_text = 'Congratulations, you have been given consent for intimacy.'
    await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    await query.edit_message_text(text="You have given consent.")
    await show_menu(query, context, new_user_id)


async def process_decline(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
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
    chat_id = update.message.chat_id
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
    user_id = str(chat_id)
    
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
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    if user_id not in referrals:
        return jsonify({'friends': []})

    friends_data = [{"user_id": friend_id, "coins": user_tokens.get(friend_id, 0)} for friend_id in referrals[user_id]]

    return jsonify({'friends': friends_data})


@app.route('/delete_friend', methods=['POST'])
def delete_friend():
    data = request.json
    if 'user_id' not in data or 'friend_id' not in data:
        return jsonify({'error': 'user_id and friend_id are required'}), 400

    user_id = str(data['user_id'])
    friend_id = str(data['friend_id'])

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
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    stars = user_stars.get(user_id, 0)
    return jsonify({'telegram_stars': stars})


# Новые эндпоинты для обработки приглашений друзей
@app.route('/send_friend_invite', methods=['POST'])
def send_friend_invite():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    
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
    data = request.json
    required_fields = ['invite_id', 'invitee_id', 'decision']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields'}), 400
    
    invite_id = data['invite_id']
    invitee_id = str(data['invitee_id'])
    decision = data['decision']
    
    # Извлекаем ID приглашающего из invite_id
    if not invite_id.startswith('friend_'):
        return jsonify({'error': 'Invalid invite ID'}), 400
    
    parts = invite_id.split('_')
    if len(parts) < 3:
        return jsonify({'error': 'Invalid invite ID format'}), 400
    
    inviter_id = parts[1]
    
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
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    
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
    data = request.json
    required_fields = ['invite_id', 'partner_id', 'decision']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields'}), 400
    
    invite_id = data['invite_id']
    partner_id = str(data['partner_id'])
    decision = data['decision']
    
    # Извлекаем ID приглашающего из invite_id
    if not invite_id.startswith('consent_'):
        return jsonify({'error': 'Invalid invite ID'}), 400
    
    parts = invite_id.split('_')
    if len(parts) < 3:
        return jsonify({'error': 'Invalid invite ID format'}), 400
    
    inviter_id = parts[1]
    
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
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    notifications = user_notifications.get(user_id, [])
    
    # Очищаем уведомления после отправки
    if user_id in user_notifications:
        user_notifications[user_id] = []
        save_data()
    
    return jsonify({'notifications': notifications})


@app.route('/get_new_friends_count', methods=['POST'])
def get_new_friends_count():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    count = new_friends_count.get(user_id, 0)
    
    return jsonify({'new_friends_count': count})


@app.route('/clear_new_friends', methods=['POST'])
def clear_new_friends():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
    if user_id in new_friends_count:
        new_friends_count[user_id] = 0
        save_data()
    
    return jsonify({'success': True})


@app.route('/get_consent_history', methods=['POST'])
def get_consent_history_endpoint():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    
    user_id = str(data['user_id'])
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
