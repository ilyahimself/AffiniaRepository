"""Telegram bot command/callback/payment handlers."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update, WebAppInfo
from telegram.ext import (CallbackContext, CallbackQueryHandler, CommandHandler, MessageHandler,
                          PreCheckoutQueryHandler, filters)

from services.database_service import DatabaseService

from ..config import BOT_USERNAME, URL
from ..invites import format_duration, issue_invite, lookup_invite
from ..security import RateLimited, utag
from ..state import (_link_both, credit_afn, data_lock, is_reserved_start_payload, is_valid_referrer,
                     referrals, user_stars, user_tokens)
from ..users import ensure_user_record, update_user_profile
from .runtime import application


async def start(update: Update, context: CallbackContext) -> None:
    args = context.args
    user = update.message.from_user
    user_id = str(user.id)
    # ВАЖНО: Сначала сохраняем профиль, потом создаём запись пользователя
    # Это нужно чтобы в уведомлении о новом пользователе был никнейм
    update_user_profile(user)
    ensure_user_record(user_id)
    
    # Логируем вход пользователя
    logging.info(f'👤 User started bot: {utag(user_id)}')
    
    if args:
        arg = args[0]
        
        # ВАЖНО: Проверяем, не является ли это зарезервированным параметром
        if is_reserved_start_payload(arg):
            # Это системный параметр (webapp, appcenter и т.д.) - просто показываем меню
            logging.info(f'{utag(user_id)} started with reserved payload: {arg}')
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
                logging.warning(f'Invalid referral link (user not found) for {utag(user_id)}')
                await update.message.reply_text("Invalid referral link.")
                await show_menu(update, context, user_id)
        else:
            # Параметр не числовой и не friend_/consent_ -> считаем системным
            logging.info(f'{utag(user_id)} started with non-numeric parameter (treated as reserved): {arg[:20]}')
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

            logging.info(f'🎉 REFERRAL SUCCESS! {utag(new_user_id)} invited by {utag(referrer_id)} | +10 / +5 AFN')

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
        invite = None
        error = reply = e.message
    if error:
        logging.info(f'Invite rejected for {utag(user_id)}: {error}')
        await update.message.reply_text(reply)
        await show_menu(update, context, user_id)
        return

    text, button = INVITE_TEXT[kind]
    if kind == 'consent' and invite and invite.duration_hours:
        text += f"\n\n⏳ Proposed duration: {format_duration(invite.duration_hours)} (auto-expires)"
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
        logging.warning(f'Unknown callback from {utag(user_id)}: {command[:40]}')


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
        logging.warning(f'Duplicate or failed payment {charge_id} for {utag(user_id)}, not crediting again')
    else:
        with data_lock:
            user_stars[user_id] = new_total
        logging.info(f'⭐ PAYMENT: {utag(user_id)} +{amount} {payment.currency} (charge {charge_id})')

    # Отправляем персонализированное сообщение
    if amount >= 1000:
        message = f"🌟 WOW! Thank you for your amazing {amount} stars donation! You're now our VIP supporter! 💎"
    elif amount >= 500:
        message = f"⭐ Thank you for your generous {amount} stars donation! You're awesome! 🎉"
    else:
        message = f"💖 Thank you for your {amount} stars donation! Every star helps us improve! ✨"

    await update.message.reply_text(message)


def setup_handlers():
    """Настройка обработчиков бота"""
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("donate", send_invoice))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    logging.info('✅ Bot handlers registered')
