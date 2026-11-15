"""Friends, partners, invitations and consent records."""
import logging
from time import time

from flask import Blueprint, jsonify, request

from services.database_service import DatabaseService

from ..invites import _qr_png_base64, format_duration, issue_invite, lookup_invite, parse_consent_options
from ..security import get_validated_user, utag
from ..state import (_link_both, _unlink_both, beloveds, consent_history, credit_afn, data_lock,
                     is_connected, new_friends_count, new_partners_count, notify, referrals,
                     sync_expired_consents, user_profiles)
from ..users import ensure_user_record, get_avatar_data_url

bp = Blueprint('relations', __name__)


@bp.route('/create_consent_link', methods=['POST'])
def create_consent_link():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    duration_hours, note, opt_err = parse_consent_options(data)
    if opt_err:
        return jsonify({'error': opt_err}), 400

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    _, consent_link = issue_invite(user_id, 'consent', duration_hours=duration_hours, note=note)
    return jsonify({'link': consent_link, 'duration_hours': duration_hours, 'note': note})


@bp.route('/generate_consent_qr', methods=['POST'])
def generate_consent_qr():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    duration_hours, note, opt_err = parse_consent_options(data)
    if opt_err:
        return jsonify({'error': opt_err}), 400

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    _, consent_link = issue_invite(user_id, 'consent', duration_hours=duration_hours, note=note)
    return jsonify({
        'consent_link': consent_link,
        'qr_code': _qr_png_base64(consent_link),
        'duration_hours': duration_hours,
        'note': note,
    })


@bp.route('/invite_info', methods=['POST'])
def invite_info():
    """Кто прислал приглашение и на каких условиях — для модального окна у получателя."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    token = data.get('invite_id', '')
    kind = 'friend' if isinstance(token, str) and token.startswith('friend_') else 'consent'
    invite, error = lookup_invite(token, kind, validated_user['user_id'])
    if error:
        return jsonify({'error': error}), 400

    inviter_id = invite.inviter_id
    profile = user_profiles.get(inviter_id, {})
    if profile.get('username'):
        display_name = f"@{profile['username']}"
    else:
        display_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or f"User {inviter_id[:6]}..."
    avatar = get_avatar_data_url(inviter_id) or f'https://api.dicebear.com/6.x/avataaars/svg?seed={inviter_id}'

    return jsonify({
        'kind': invite.kind,
        'inviter_id': inviter_id,
        'display_name': display_name,
        'avatar': avatar,
        'duration_hours': invite.duration_hours,
        'note': invite.note,
    })


@bp.route('/get_beloved', methods=['POST'])
def get_beloved():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    sync_expired_consents()
    if user_id not in beloveds:
        return jsonify({'beloved': []})

    beloved_data = []
    with data_lock:
        partner_ids = list(beloveds.get(user_id, []))
        history = list(consent_history.get(user_id, []))
    for b_id in partner_ids:
        # Статус и дата — из реальных записей consents
        pair = [c for c in history if c.get('partner_id') == b_id]
        active = [c for c in pair if c.get('status') == 'active']
        act_rec = active[-1] if active else None
        last_rec = pair[-1] if pair else None
        relationship_status = 'current' if active else ('past' if pair else 'potential')
        added_ts = min((c.get('timestamp', 0) for c in pair), default=0)

        beloved_data.append({
            "user_id": b_id,
            "relationship_status": relationship_status,
            "trust_level": "partner",
            "added_date": added_ts * 1000 if added_ts else None,  # мс для фронтенда
            "consent_type": "intimate",
            "active_consent_id": act_rec.get('id') if act_rec else None,
            "active_expires_at": act_rec.get('expires_at') if act_rec else None,
            "active_duration_hours": act_rec.get('duration_hours') if act_rec else None,
            "active_note": act_rec.get('note') if act_rec else None,
            "last_consent_status": last_rec.get('status') if last_rec else None,
            "last_consent_at": max((c.get('timestamp', 0) for c in pair), default=None),
        })

    return jsonify({'beloved': beloved_data})


@bp.route('/delete_beloved', methods=['POST'])
def delete_beloved():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    if 'beloved_id' not in data:
        return jsonify({'error': 'beloved_id is required'}), 400
    beloved_id = str(data['beloved_id'])
    ensure_user_record(user_id)

    if beloved_id in beloveds.get(user_id, []):
        revoked_ids = set(DatabaseService.revoke_active_consents_between(user_id, beloved_id))
        if revoked_ids:
            revoked_ts = int(time())
            with data_lock:
                for uid in (user_id, beloved_id):
                    for record in consent_history.get(uid, []):
                        if record.get('id') in revoked_ids:
                            record['status'] = 'revoked'
                            record['revoked_at'] = revoked_ts
            notify(beloved_id, 'consent_revoked', user_id, 'A consent with you has been withdrawn.')
        DatabaseService.remove_partner(user_id, beloved_id)
        _unlink_both(beloveds, user_id, beloved_id)
        return jsonify({'success': True})
    return jsonify({'error': 'Beloved not found'}), 400


@bp.route('/get_friends', methods=['POST'])
def get_friends():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    if user_id not in referrals:
        return jsonify({'friends': []})

    emergency = set(DatabaseService.get_emergency_contacts(user_id))
    with data_lock:
        friend_ids = list(referrals.get(user_id, []))
    friends_data = [
        {"user_id": friend_id, "trust_level": "emergency" if friend_id in emergency else "friend"}
        for friend_id in friend_ids
    ]

    return jsonify({'friends': friends_data})


@bp.route('/delete_friend', methods=['POST'])
def delete_friend():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    if 'friend_id' not in data:
        return jsonify({'error': 'friend_id is required'}), 400
    friend_id = str(data['friend_id'])
    ensure_user_record(user_id)

    if friend_id in referrals.get(user_id, []):
        DatabaseService.remove_friend(user_id, friend_id)
        _unlink_both(referrals, user_id, friend_id)
        return jsonify({'success': True})

    return jsonify({'error': 'Friend not found'}), 400


@bp.route('/user_card', methods=['POST'])
def user_card():
    """Имя и аватар другого пользователя — только для связанных с запрашивающим."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    target_id = str(data.get('target_id', '')).strip()
    if not target_id.isdigit():
        return jsonify({'error': 'target_id is required'}), 400

    if not is_connected(user_id, target_id):
        return jsonify({'error': 'Not allowed'}), 403

    profile = user_profiles.get(target_id, {})
    username = profile.get('username', '')
    first_name = profile.get('first_name', '')
    last_name = profile.get('last_name', '')
    if username:
        display_name = f'@{username}'
    elif first_name or last_name:
        display_name = f'{first_name} {last_name}'.strip()
    else:
        display_name = f'User {target_id[:6]}...'

    avatar = get_avatar_data_url(target_id) or f'https://api.dicebear.com/6.x/avataaars/svg?seed={target_id}'

    return jsonify({'user_id': target_id, 'display_name': display_name, 'avatar': avatar})


@bp.route('/send_friend_invite', methods=['POST'])
def send_friend_invite():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)

    _, invite_link = issue_invite(user_id, 'friend')

    return jsonify({
        'invite_link': invite_link,
        'message': "Want to be my friend in Affinia? 🌟"
    })


@bp.route('/handle_friend_invite', methods=['POST'])
def handle_friend_invite():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    invitee_id = validated_user['user_id']

    if 'invite_id' not in data or 'decision' not in data:
        return jsonify({'error': 'Missing required fields'}), 400

    invite_id = data['invite_id']
    decision = data['decision']
    ensure_user_record(invitee_id)

    # Инвайтер берётся из записи в БД, а не из строки токена
    invite, error = lookup_invite(invite_id, 'friend', invitee_id)
    if error:
        return jsonify({'error': error}), 400
    inviter_id = invite.inviter_id

    if decision == 'accept':
        tokens_earned = 0
        # add_friend возвращает True только для новой связи — награда и уведомление один раз
        if DatabaseService.add_friend(inviter_id, invitee_id):
            _link_both(referrals, inviter_id, invitee_id)
            credit_afn(inviter_id, 50, 'friend_invite_inviter', invite_id)
            credit_afn(invitee_id, 25, 'friend_invite_accepted', invite_id)
            tokens_earned = 25
            DatabaseService.bump_unseen(inviter_id, friends=1)
            with data_lock:
                new_friends_count[inviter_id] = new_friends_count.get(inviter_id, 0) + 1
            notify(inviter_id, 'friend_accepted', invitee_id, 'User accepted your friend request! 🎉')
        DatabaseService.mark_invite_used(invite_id, invitee_id, 'accept')

        return jsonify({
            'success': True,
            'message': 'Friend request accepted!',
            'tokens_earned': tokens_earned
        })

    elif decision == 'decline':
        notify(inviter_id, 'friend_declined', invitee_id, 'User declined your friend request 😔')
        DatabaseService.mark_invite_used(invite_id, invitee_id, 'decline')

        return jsonify({
            'success': True,
            'message': 'Friend request declined'
        })

    return jsonify({'error': 'Invalid decision'}), 400


@bp.route('/send_consent_invite', methods=['POST'])
def send_consent_invite():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    duration_hours, note, opt_err = parse_consent_options(data)
    if opt_err:
        return jsonify({'error': opt_err}), 400

    user_id = validated_user['user_id']
    ensure_user_record(user_id)

    _, invite_link = issue_invite(user_id, 'consent', duration_hours=duration_hours, note=note)
    if duration_hours:
        message = f"Ready to connect? ({format_duration(duration_hours)} mutual consent) 💕"
    else:
        message = "Ready to explore an intimate connection? 💕"

    return jsonify({
        'invite_link': invite_link,
        'message': message,
        'duration_hours': duration_hours,
        'note': note,
    })


@bp.route('/handle_consent_invite', methods=['POST'])
def handle_consent_invite():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    partner_id = validated_user['user_id']

    if 'invite_id' not in data or 'decision' not in data:
        return jsonify({'error': 'Missing required fields'}), 400

    invite_id = data['invite_id']
    decision = data['decision']
    ensure_user_record(partner_id)
    sync_expired_consents()

    # Инвайтер берётся из записи в БД, а не из строки токена
    invite, error = lookup_invite(invite_id, 'consent', partner_id)
    if error:
        return jsonify({'error': error}), 400
    inviter_id = invite.inviter_id

    if decision == 'accept':
        tokens_earned = 0
        # Награда +100 AFN начисляется только при первом согласии между этими двумя людьми
        first_time_pair = not DatabaseService.has_prior_consent(inviter_id, partner_id)
        if DatabaseService.add_partner(inviter_id, partner_id):
            _link_both(beloveds, inviter_id, partner_id)

        if first_time_pair:
            credit_afn(inviter_id, 100, 'consent_inviter', invite_id)
            credit_afn(partner_id, 100, 'consent_accepted', invite_id)
            tokens_earned = 100

        consent_recorded = False
        # Если предыдущее согласие истекло или отозвано, создаётся новая активная запись;
        # если активное согласие уже есть, дубликат не плодится.
        if not DatabaseService.has_active_consent(inviter_id, partner_id):
            consent = DatabaseService.add_consent(
                inviter_id, partner_id, 'intimate',
                duration_hours=invite.duration_hours, note=invite.note,
            )
            base = {k: v for k, v in consent.items() if k not in ('inviter_id', 'partner_id')}
            with data_lock:
                consent_history.setdefault(inviter_id, []).append({**base, 'partner_id': partner_id})
                consent_history.setdefault(partner_id, []).append({**base, 'partner_id': inviter_id})
            consent_recorded = True

            if first_time_pair:
                DatabaseService.bump_unseen(inviter_id, partners=1)
                with data_lock:
                    new_partners_count[inviter_id] = new_partners_count.get(inviter_id, 0) + 1
            notify(inviter_id, 'consent_accepted', partner_id, 'Your intimate connection request was accepted! 💕')
            logging.info(f'❤️ CONSENT ACCEPTED: {utag(partner_id)} ← {utag(inviter_id)}')

        DatabaseService.mark_invite_used(invite_id, partner_id, 'accept')

        return jsonify({
            'success': True,
            'message': 'Consent request accepted!',
            'tokens_earned': tokens_earned,
            'consent_recorded': consent_recorded,
        })

    elif decision == 'decline':
        notify(inviter_id, 'consent_declined', partner_id, 'Your intimate connection request was declined 💔')
        DatabaseService.mark_invite_used(invite_id, partner_id, 'decline')

        return jsonify({
            'success': True,
            'message': 'Consent request declined'
        })

    return jsonify({'error': 'Invalid decision'}), 400


@bp.route('/revoke_consent', methods=['POST'])
def revoke_consent():
    """Отзыв согласия одной из сторон. Запись не удаляется — меняется статус."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    try:
        consent_id = int(data.get('consent_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'consent_id is required'}), 400

    sync_expired_consents()
    if not DatabaseService.revoke_consent(consent_id, user_id):
        return jsonify({'error': 'Consent not found'}), 404

    revoked_ts = int(time())
    other_id = None
    was_active = False
    with data_lock:
        for record in consent_history.get(user_id, []):
            if record.get('id') == consent_id:
                other_id = record.get('partner_id')
                if record.get('status') == 'active':
                    record['status'] = 'revoked'
                    record['revoked_at'] = revoked_ts
                    was_active = True
        if other_id and was_active:
            for record in consent_history.get(other_id, []):
                if record.get('id') == consent_id:
                    record['status'] = 'revoked'
                    record['revoked_at'] = revoked_ts

    if other_id and was_active:
        notify(other_id, 'consent_revoked', user_id, 'A consent with you has been withdrawn.')
        logging.info(f'🚫 CONSENT REVOKED: id={consent_id} by {utag(user_id)}')

    return jsonify({'success': True})


@bp.route('/get_consent_history', methods=['POST'])
def get_consent_history_endpoint():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    sync_expired_consents()
    with data_lock:
        history = [dict(h) for h in consent_history.get(user_id, [])]

    total_consents = len(history)
    active_consents = len([h for h in history if h.get('status') == 'active'])
    expired_consents = len([h for h in history if h.get('status') == 'expired'])
    this_month_consents = len([h for h in history if h.get('timestamp', 0) > time() - 2592000])  # 30 дней

    return jsonify({
        'consent_history': history,
        'total_consents': total_consents,
        'active_consents': active_consents,
        'expired_consents': expired_consents,
        'this_month_consents': this_month_consents,
    })
