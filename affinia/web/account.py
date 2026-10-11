"""The caller's own account: balance, profile sync, referral link, stars, notifications, data rights."""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from services.database_service import DatabaseService

from ..config import BOT_USERNAME
from ..invites import _qr_png_base64
from ..security import enforce_rate_limit, get_validated_user, utag
from ..state import (_avatar_cache, data_lock, load_data, new_friends_count, user_notifications,
                     user_profiles, user_stars, user_tokens)
from ..users import ensure_user_record

bp = Blueprint('account', __name__)


@bp.route('/get_balance', methods=['POST'])
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
        logging.info(f'👤 Profile updated from Mini App: {utag(user_id)}')
        try:
            DatabaseService.update_profile(user_id, **profile)
        except Exception as e:
            logging.error(f"DB error saving profile: {e}")
    
    with data_lock:
        balance = user_tokens.get(user_id, 0)
    
    logging.info(f'💰 Balance check: {utag(user_id)} → {balance} AFN')
    return jsonify({'balance': balance})


@bp.route('/create_referral_link', methods=['POST'])
def create_referral_link():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    logging.info(f'🔗 Referral link created: {utag(user_id)}')
    return jsonify({'link': referral_link})


@bp.route('/generate_referral_qr', methods=['POST'])
def generate_referral_qr():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return jsonify({'referral_link': referral_link, 'qr_code': _qr_png_base64(referral_link)})


@bp.route('/get_telegram_stars', methods=['POST'])
def get_telegram_stars():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    stars = user_stars.get(user_id, 0)
    return jsonify({'telegram_stars': stars})


@bp.route('/get_notifications', methods=['POST'])
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


@bp.route('/get_new_friends_count', methods=['POST'])
def get_new_friends_count():
    data = request.json or {}
    
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    
    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    count = new_friends_count.get(user_id, 0)
    
    return jsonify({'new_friends_count': count})


@bp.route('/clear_new_friends', methods=['POST'])
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


@bp.route('/export_my_data', methods=['POST'])
def export_my_data():
    """Всё, что хранится о пользователе, в виде JSON (право на доступ)."""
    data = request.json or {}
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    user_id = validated_user['user_id']
    enforce_rate_limit(f"export:{user_id}", 5, 3600)
    ensure_user_record(user_id)
    export = DatabaseService.export_user(user_id)
    if export is None:
        return jsonify({'error': 'User not found'}), 404
    export['exported_at'] = datetime.now(timezone.utc).isoformat()
    logging.info(f'📤 Data export: {utag(user_id)}')
    return jsonify(export)


@bp.route('/delete_account', methods=['POST'])
def delete_account():
    """Удаление аккаунта и данных (право на удаление). Необратимо."""
    data = request.json or {}
    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401
    if data.get('confirm') != 'DELETE':
        return jsonify({'error': 'Confirmation required'}), 400
    user_id = validated_user['user_id']

    try:
        deleted = DatabaseService.delete_user(user_id)
    except Exception as e:
        logging.error(f'Account deletion failed for {utag(user_id)}: {e}')
        return jsonify({'error': 'Deletion failed, please try again later'}), 500
    if not deleted:
        return jsonify({'error': 'User not found'}), 404

    _avatar_cache.pop(user_id, None)
    load_data()  # кэш пересобирается из БД: связи и история у второй стороны обновляются тоже
    logging.info(f'🗑️ Account deleted: {utag(user_id)}')
    return jsonify({'success': True})
