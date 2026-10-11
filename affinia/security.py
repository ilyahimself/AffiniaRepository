"""Request authentication (Telegram initData), rate limiting and log pseudonyms."""
import hashlib
import hmac
import json
import logging
import threading
from collections import defaultdict
from time import time
from urllib.parse import parse_qs, unquote

from flask import request

from .config import API_TOKEN, AUTH_FAIL_LIMIT, CLOCK_SKEW, INIT_DATA_MAX_AGE, LOG_SALT, USER_RATE_LIMIT


class RateLimited(Exception):
    """Поднимается из любого места обработчика; превращается в 429 через errorhandler."""
    def __init__(self, message='Too many requests. Please try again later.'):
        super().__init__(message)
        self.message = message


_rate_buckets = defaultdict(list)


_rate_lock = threading.Lock()


_rate_calls = 0


def enforce_rate_limit(key: str, limit: int, window: int, message: str = None) -> None:
    """Регистрирует обращение по ключу; при превышении лимита поднимает RateLimited."""
    global _rate_calls
    now = time()
    with _rate_lock:
        bucket = _rate_buckets[key]
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            bucket_name, _, subject = key.partition(':')
            logging.warning(f'🛡️ rate limit exceeded: {bucket_name}:{utag(subject)}')
            raise RateLimited(message or RateLimited().message)
        bucket.append(now)
        # Периодически выбрасываем протухшие ключи, чтобы словарь не рос бесконечно
        _rate_calls += 1
        if _rate_calls % 2000 == 0:
            stale = [k for k, b in _rate_buckets.items() if not b or now - b[-1] > 3600]
            for k in stale:
                del _rate_buckets[k]


def utag(user_id) -> str:
    """Псевдоним пользователя для логов: стабильный, но не раскрывает Telegram ID и имя.

    Логи уходят на сторону хостинга; по тегу можно связать события одного пользователя
    при отладке, а восстановить ID без LOG_SALT нельзя.
    """
    if not user_id:
        return 'u:none'
    digest = hmac.new(LOG_SALT.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f'u:{digest[:10]}'


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
            logging.warning('🛡️ initData validation failed: hash mismatch')
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
