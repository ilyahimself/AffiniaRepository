"""User records and profiles: creation, profile sync from Telegram, avatars."""
import logging
from base64 import b64encode
from time import time

from services.database_service import DatabaseService

from .bot.runtime import application, run_bot_coro
from .config import ADMIN_ID, AVATAR_CACHE_MAX, AVATAR_CACHE_TTL
from .security import utag
from .state import _avatar_cache, data_lock, get_user_display_name, user_profiles, user_tokens


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
        logging.info(f'🆕 New user registered: {utag(user_id)}')

        # Уведомление админу о новом пользователе
        if ADMIN_ID and user_id != ADMIN_ID:
            display_name = get_user_display_name(user_id)
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
        logging.warning(f'Avatar fetch failed for {utag(user_id)}: {e}')
        data_url = None
    if len(_avatar_cache) >= AVATAR_CACHE_MAX:
        _avatar_cache.clear()
    _avatar_cache[user_id] = (now + AVATAR_CACHE_TTL, data_url)
    return data_url
