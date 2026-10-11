"""Telegram Stars invoice links."""
import logging

from flask import Blueprint, jsonify, request
from telegram import LabeledPrice

from ..bot.runtime import application, run_bot_coro
from ..security import enforce_rate_limit, get_validated_user, utag
from ..users import ensure_user_record

bp = Blueprint('payments', __name__)


@bp.route('/get_donate_link', methods=['POST'])
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
        logging.warning(f'Invalid donation amount from {utag(user_id)}: {amount}')
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
        logging.info(f'Donation invoice created for {utag(user_id)}: {amount} stars, tier: {tier}')
        return jsonify({'success': True, 'invoice_link': invoice_link})
    except Exception as e:
        logging.error(f"Error creating invoice for {utag(user_id)}: {e}")
        return jsonify({'error': 'Failed to create donation invoice'}), 500
