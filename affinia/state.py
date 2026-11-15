"""In-memory read cache mirrored from PostgreSQL, and the helpers that keep it consistent.

PostgreSQL is the source of truth; these dicts are rebuilt on start (load_data) and updated
write-through by the request handlers. Never rebind them: other modules import the objects.
"""
import logging
import os
import sys
import threading

from services.database_service import DatabaseService

from .config import RESERVED_START_PAYLOADS


user_tokens = {}
referrals = {}
beloveds = {}
user_stars = {}
user_profiles = {}
user_compliance = {}  # Подтверждение 18+ и явного согласия по ст. 9 GDPR
pending_friend_requests = {}  # Хранение ожидающих запросов дружбы
pending_consent_requests = {}  # Хранение ожидающих запросов партнерства
user_notifications = {}  # Хранение уведомлений для пользователей
new_friends_count = {}  # Счетчик новых непросмотренных друзей
new_partners_count = {}  # Счетчик новых непросмотренных партнеров
consent_history = {}  # История всех consent связей
data_lock = threading.Lock()
_avatar_cache: dict = {}

_CACHES = (
    (user_tokens, 'user_tokens'), (referrals, 'referrals'), (beloveds, 'beloveds'),
    (user_stars, 'user_stars'), (user_profiles, 'user_profiles'), (user_compliance, 'user_compliance'),
    (pending_friend_requests, 'pending_friend_requests'), (pending_consent_requests, 'pending_consent_requests'),
    (user_notifications, 'user_notifications'), (new_friends_count, 'new_friends_count'),
    (new_partners_count, 'new_partners_count'), (consent_history, 'consent_history'),
)


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
    """Rebuild the in-memory read cache from PostgreSQL (in place, so imported references stay valid)."""
    init_database()
    snap = DatabaseService.load_snapshot()
    with data_lock:
        for cache, key in _CACHES:
            cache.clear()
            cache.update(snap[key])
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


def get_user_display_name(user_id: str) -> str:
    """Имя пользователя для сообщений людям (например, админу). В логи не писать — есть utag()."""
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


def is_connected(user_id: str, target_id: str) -> bool:
    """Связаны ли пользователи (друзья или партнёры)."""
    if user_id == target_id:
        return True
    return target_id in referrals.get(user_id, []) or target_id in beloveds.get(user_id, [])


def sync_expired_consents() -> list[int]:
    """Переводит истёкшие срочные согласия в статус 'expired' в PostgreSQL и в кэше."""
    expired_ids = DatabaseService.expire_due_consents()
    if expired_ids:
        expired_set = set(expired_ids)
        with data_lock:
            for records in consent_history.values():
                for rec in records:
                    if rec.get('id') in expired_set and rec.get('status') == 'active':
                        rec['status'] = 'expired'
    return expired_ids
