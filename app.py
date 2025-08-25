import os
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

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

user_tokens = {}
referrals = {}
beloveds = {}
user_stars = {}
data_lock = threading.Lock()

API_TOKEN = os.getenv('API_TOKEN', 'REDACTED_BOT_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'AffiniaBot')
bot = Bot(token=API_TOKEN)
URL = os.getenv('WEBAPP_URL', "https://d673-195-133-94-131.ngrok-free.app")

# Создаём приложение бота
application = Application.builder().token(API_TOKEN).connection_pool_size(50).build()


def load_data():
    global user_tokens, referrals, beloveds, user_stars
    try:
        with open('user_tokens.json', 'r') as f:
            data = json.load(f)
            user_tokens = data.get('user_tokens', {})
            referrals = data.get('referrals', {})
            beloveds = data.get('beloveds', {})
            user_stars = data.get('user_stars', {})  # Новая строка
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
                    'user_stars': user_stars  # Новая строка
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


@app.route('/get_balance', methods=['POST'])
def get_balance():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    user_id = str(data['user_id'])
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    return jsonify({'balance': balance})


@app.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400
    user_id = str(data['user_id'])
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
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
    beloved_data = [{"user_id": b_id} for b_id in beloveds[user_id]]
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
    amount = 100  # 100 звезд

    # Запускаем корутину в event loop, сохранённом в bot_loop
    future = asyncio.run_coroutine_threadsafe(
        bot.create_invoice_link(
            title="Support the App",
            description="Donate using Telegram Stars!",
            payload=f"donation_{user_id}",
            provider_token="",  # Если для XTR требуется оставить пустым, иначе вставьте нужный токен
            currency="XTR",     # Проверьте, поддерживается ли валюта "XTR"
            prices=[LabeledPrice(label="Donation", amount=amount)]
        ),
        bot_loop  # Используем сохранённый event loop
    )

    try:
        invoice_link = future.result(timeout=10)
        return jsonify({'success': True, 'invoice_link': invoice_link})
    except Exception as e:
        logging.error(f"Error creating invoice: {e}")
        return jsonify({'error': f'Failed to create donation invoice: {e}'}), 500

# ===== Хендлеры бота =====

async def start(update: Update, context: CallbackContext) -> None:
    args = context.args
    user_id = str(update.message.from_user.id)
    if args:
        if args[0].startswith("consent_"):
            await handle_consent(update, context, args[0].split("_")[1])
        else:
            await referral_start(update, context, args[0])
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
    balance = user_tokens.get(user_id, 0)
    if balance >= 5:
        user_tokens[user_id] = balance - 5
        save_data()
        consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
        text = f'Share this link to request consent: {consent_link}'
    else:
        text = 'Insufficient balance to create consent link.'
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


# Главное: запускаем бота СИНХРОННО без asyncio.run(...)
def run_bot():
    global bot_loop
    # Получаем event loop, который будет использоваться для polling
    bot_loop = asyncio.get_event_loop()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("donate", send_invoice))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    
    application.run_polling()

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

    # Обновляем баланс звёзд пользователя
    user_stars[user_id] = user_stars.get(user_id, 0) + 100
    save_data()

    await update.message.reply_text("Thank you for your donation! Your stars have been added.")


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


if __name__ == '__main__':
    load_data()
    # load_beloveds()

    # Flask в отдельном потоке
    flask_thread = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False),
        daemon=True
    )
    flask_thread.start()

    # Запускаем бота в основном потоке синхронно
    try:
        run_bot()
    except KeyboardInterrupt:
        print("Bot stopped by user (Ctrl+C)")
        # Если надо - дожидаемся Flask
        flask_thread.join()
