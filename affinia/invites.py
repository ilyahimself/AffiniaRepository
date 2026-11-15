"""Server-issued single-use invitation tokens (friend / consent) and QR rendering."""
import secrets
from base64 import b64encode
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import qrcode

from services.database_service import DatabaseService

from .config import BOT_USERNAME, CONSENT_MAX_DURATION_HOURS, CONSENT_NOTE_MAX_LEN, INVITE_TTL
from .security import enforce_rate_limit
from .state import data_lock, pending_consent_requests, pending_friend_requests


def format_duration(hours: Optional[int]) -> str:
    """Human-readable label for a consent duration in hours."""
    if not hours:
        return 'ongoing'
    if hours == 1:
        return '1 hour'
    if hours >= 48 and hours % 24 == 0:
        return f'{hours // 24} days'
    return f'{hours} hours'


def parse_consent_options(data: dict) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Validate optional duration_hours and note on consent-link creation.

    Returns (duration_hours, note, error_message).
    """
    raw_dur = data.get('duration_hours')
    duration_hours: Optional[int] = None
    if raw_dur not in (None, '', 0):
        if isinstance(raw_dur, bool) or not isinstance(raw_dur, int):
            return None, None, 'duration_hours must be an integer number of hours'
        if raw_dur < 1 or raw_dur > CONSENT_MAX_DURATION_HOURS:
            return None, None, f'duration_hours must be between 1 and {CONSENT_MAX_DURATION_HOURS}'
        duration_hours = raw_dur

    raw_note = data.get('note')
    note: Optional[str] = None
    if raw_note is not None:
        if not isinstance(raw_note, str):
            return None, None, 'note must be a string'
        cleaned = ' '.join(raw_note.split())
        if len(cleaned) > CONSENT_NOTE_MAX_LEN:
            return None, None, f'note must be at most {CONSENT_NOTE_MAX_LEN} characters'
        note = cleaned or None

    return duration_hours, note, None


def issue_invite(user_id: str, kind: str, duration_hours: Optional[int] = None,
                 note: Optional[str] = None) -> tuple[str, str]:
    """Создаёт инвайт в БД. Возвращает (token, deep link). Префикс kind_ нужен боту и фронту для диспетчеризации."""
    # Ограничивает и спам ссылками, и рост таблицы invites
    enforce_rate_limit(f"invite_issue:{user_id}", 30, 3600, 'Too many invitations created. Please try again later.')
    token = f"{kind}_{secrets.token_urlsafe(16)}"
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + INVITE_TTL[kind]
    if not DatabaseService.create_invite(token, user_id, kind, expires_at, duration_hours=duration_hours, note=note):
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


def _qr_png_base64(payload: str) -> str:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return b64encode(buffered.getvalue()).decode('utf-8')
