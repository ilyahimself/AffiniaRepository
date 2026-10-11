"""Server-issued single-use invitation tokens (friend / consent) and QR rendering."""
import secrets
from base64 import b64encode
from datetime import datetime, timezone
from io import BytesIO

import qrcode

from services.database_service import DatabaseService

from .config import BOT_USERNAME, INVITE_TTL
from .security import enforce_rate_limit
from .state import data_lock, pending_consent_requests, pending_friend_requests


def issue_invite(user_id: str, kind: str) -> tuple[str, str]:
    """Создаёт инвайт в БД. Возвращает (token, deep link). Префикс kind_ нужен боту и фронту для диспетчеризации."""
    # Ограничивает и спам ссылками, и рост таблицы invites
    enforce_rate_limit(f"invite_issue:{user_id}", 30, 3600, 'Too many invitations created. Please try again later.')
    token = f"{kind}_{secrets.token_urlsafe(16)}"
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + INVITE_TTL[kind]
    if not DatabaseService.create_invite(token, user_id, kind, expires_at):
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
