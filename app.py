import os
import json
import logging
import threading
import asyncio
from flask import Flask, render_template, request, jsonify, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler
from json.decoder import JSONDecodeError

logging.basicConfig(level=logging.INFO)

# Инициализация Flask-приложения
app = Flask(__name__)



# Глобальная переменная для хранения токенов пользователей
user_tokens = {}
data_lock = threading.Lock()  # Блокировка для защиты доступа к user_tokens

# Инициализация бота Telegram
API_TOKEN = os.getenv('API_TOKEN', 'REDACTED_BOT_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'AffiniaBot')
application = Application.builder().token(API_TOKEN).build()

# Функция для загрузки данных из файла
def load_data():
    global user_tokens
    try:
        with open('user_tokens.json', 'r') as f:
            data = json.load(f)
            user_tokens = data.get('user_tokens', {})
            print(f"Loaded data: {user_tokens}")
    except FileNotFoundError:
        user_tokens = {}
        print("Data file not found, starting with an empty dictionary")
    except JSONDecodeError:
        logging.error('Error decoding JSON from user_tokens.json. Starting with empty data.')
        user_tokens = {}
    except Exception as e:
        logging.error(f"Unexpected error while loading data: {e}")
        user_tokens = {}

# Функция для сохранения данных в файл
def save_data():
    try:
        with data_lock:
            with open('user_tokens.json', 'w') as f:
                json.dump({'user_tokens': user_tokens}, f)
        print(f"Saved data: {user_tokens}")
    except Exception as e:
        logging.error(f"Unexpected error while saving data: {e}")
        
# Маршрут для отображения HTML-шаблона
@app.route('/mini_app')
def mini_app():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/static/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

    @app.route('/mini_app')
def mini_app():
    return render_template('index.html')



# Маршрут для получения баланса пользователя
@app.route('/get_balance', methods=['POST'])
def get_balance():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    return jsonify({'balance': balance})

# Маршрут для создания реферальной ссылки
@app.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return jsonify({'link': referral_link})

# Маршрут для создания ссылки на согласие
@app.route('/create_consent_link', methods=['POST'])
def create_consent_link():
    data = request.json
    if 'user_id' not in data:
        return jsonify({'error': 'user_id is required'}), 400

    user_id = str(data['user_id'])
    consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
    return jsonify({'link': consent_link})

# Обработка команды /start
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

# Обработка старта по реферальной ссылке
async def referral_start(update: Update, context: CallbackContext, referrer_id: str) -> None:
    new_user_id = str(update.message.from_user.id)
    if referrer_id and referrer_id != new_user_id:
        user_tokens[referrer_id] = user_tokens.get(referrer_id, 0) + 10
        user_tokens[new_user_id] = user_tokens.get(new_user_id, 0) + 5
        save_data()
        referrer_text = 'You have been credited with 10 AffinyCoin for inviting a new user.'
        new_user_text = 'Thank you for registering! You have been credited with 5 AffinyCoin.\nYour referrer has been credited with 10 AffinyCoin.'
        await update.message.reply_text(new_user_text)
        await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    else:
        if referrer_id == new_user_id:
            error_text = 'You cannot use your own referral link.'
        else:
            error_text = 'Invalid referral link.'
        await update.message.reply_text(error_text)
    await show_menu(update, context, new_user_id)

# Обработка согласия
async def handle_consent(update: Update, context: CallbackContext, referrer_id: str) -> None:
    new_user_id = str(update.message.from_user.id)
    consent_text = "Consent"
    decline_text = "Decline"

    keyboard = [
        [InlineKeyboardButton(consent_text, callback_data=f'consent_{referrer_id}_{new_user_id}'), 
         InlineKeyboardButton(decline_text, callback_data=f'decline_{referrer_id}_{new_user_id}')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    choice_text = 'Please choose:'
    await update.message.reply_text(choice_text, reply_markup=reply_markup)

# Меню бота
async def show_menu(update, context, user_id=None):
    if user_id is None:
        user_id = str(update.message.from_user.id if hasattr(update.message, 'from_user') else update.from_user.id)

    keyboard = [
        [InlineKeyboardButton("My Balance", callback_data='balance')],
        [InlineKeyboardButton("Referral Link", callback_data='create_referral')],
        [InlineKeyboardButton("Consent Link", callback_data='create_consent')],
        [InlineKeyboardButton("Open App", web_app=WebAppInfo(url="https://affiniabot-6eb338ddee5e.herokuapp.com/mini_app"))],
    ]
    text = 'Please choose:'
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update, 'message'):
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.edit_message_text(text, reply_markup=reply_markup)
# Обработка нажатия кнопок
async def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    command = query.data
    user_id = str(query.from_user.id)
    
    if command == 'balance':
        await balance(query, context, callback_query=True)
    elif command == 'create_referral':
        await create_referral_link(query, context)
    elif command == 'create_consent':
        await create_consent_link(query, context)
    elif command.startswith('consent_'):
        await process_consent(query, context, command)
    elif command.startswith('decline_'):
        await process_decline(query, context, command)

# Функция обработки баланса
async def balance(update: Update, context: CallbackContext, callback_query=False) -> None:
    user_id = str(update.message.from_user.id if not callback_query else update.from_user.id)
    balance = user_tokens.get(user_id, 0)
    text = f'Your balance: {balance} AffinyCoin.'
    if callback_query:
        await update.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    await show_menu(update, context, user_id)

# Создание реферальной ссылки
async def create_referral_link(update: Update, context: CallbackContext) -> None:
    user_id = str(update.from_user.id if hasattr(update, 'message') else update.from_user.id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = f'Share this link to invite friends and earn tokens: {referral_link}'
    if hasattr(update, 'message'):
        await update.message.reply_text(text)
    else:
        await update.edit_message_text(text)
    await show_menu(update, context, user_id)

# Создание ссылки на согласие
async def create_consent_link(update: Update, context: CallbackContext) -> None:
    user_id = str(update.from_user.id if hasattr(update, 'message') else update.from_user.id)
    balance = user_tokens.get(user_id, 0)
    if balance >= 5:
        user_tokens[user_id] = balance - 5
        save_data()
        consent_link = f"https://t.me/{BOT_USERNAME}?start=consent_{user_id}"
        text = f'Share this link to request consent: {consent_link}'
    else:
        text = 'Insufficient balance to create consent link.'
    if hasattr(update, 'message'):
        await update.message.reply_text(text)
    else:
        await update.edit_message_text(text)
    await show_menu(update, context, user_id)

# Обработка согласия
async def process_consent(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
    referrer_text = 'Congratulations, you have been given consent for intimacy.'
    await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    await query.edit_message_text(text="You have given consent.")
    await show_menu(query, context, new_user_id)

# Обработка отказа
async def process_decline(query, context, command):
    _, referrer_id, new_user_id = command.split('_')
    referrer_text = 'Sorry, you have been declined.'
    await context.bot.send_message(chat_id=referrer_id, text=referrer_text)
    await query.edit_message_text(text="You have declined consent.")
    await show_menu(query, context, new_user_id)

# Функция запуска бота
def run_bot():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    asyncio.run(application.run_polling())

if __name__ == '__main__':
    load_data()

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False))
    flask_thread.start()

    # Запуск бота в основном потоке
    run_bot()
