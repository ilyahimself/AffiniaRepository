"""Safety features that actually do something: emergency contacts, SOS, learning missions."""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from services.database_service import DatabaseService

from ..bot.runtime import application, run_bot_coro
from ..security import enforce_rate_limit, get_validated_user, utag
from ..state import data_lock, get_user_display_name, notify, referrals, user_tokens
from ..users import ensure_user_record

bp = Blueprint('safety', __name__)

# mission_id -> AFN reward. Eligibility is checked server-side in _mission_allowed().
MISSIONS = {
    'consent_basics': 15,
    'legal_rights': 20,
    'emergency_protocols': 25,
    'first_referral': 30,
    'trust_network': 40,
    'first_donation': 0,  # "Supporter" badge only; no points for paying
}

SOS_LIMIT = (5, 3600)


def _friend_count(user_id: str) -> int:
    with data_lock:
        return len(referrals.get(user_id, []))


def _mission_allowed(user_id: str, mission_id: str) -> tuple[bool, str]:
    if mission_id == 'first_referral':
        return (_friend_count(user_id) >= 1, 'Add at least one friend first')
    if mission_id == 'trust_network':
        return (_friend_count(user_id) >= 3, 'You need 3 friends in your trust network')
    if mission_id == 'first_donation':
        return (DatabaseService.count_payments(user_id) >= 1, 'No donation found yet')
    return (True, '')


@bp.route('/set_emergency_contact', methods=['POST'])
def set_emergency_contact():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    friend_id = str(data.get('friend_id', '')).strip()
    if not friend_id.isdigit():
        return jsonify({'error': 'friend_id is required'}), 400
    enabled = bool(data.get('enabled', True))

    ensure_user_record(user_id)
    with data_lock:
        is_friend = friend_id in referrals.get(user_id, [])
    if not is_friend:
        return jsonify({'error': 'Friend not found'}), 400

    if not DatabaseService.set_emergency_contact(user_id, friend_id, enabled):
        return jsonify({'error': 'Could not update emergency contact'}), 500

    if enabled:
        notify(friend_id, 'emergency_contact', user_id,
               f"{get_user_display_name(user_id)} added you as an emergency contact")
    logging.info(f"🆘 {utag(user_id)} {'added' if enabled else 'removed'} emergency contact {utag(friend_id)}")
    return jsonify({'success': True, 'emergency_contacts': DatabaseService.get_emergency_contacts(user_id)})


@bp.route('/sos', methods=['POST'])
def sos():
    """Alert the user's emergency contacts via the bot. The call to 112 happens on the device."""
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    enforce_rate_limit(f"sos:{user_id}", *SOS_LIMIT,
                       message='Too many SOS alerts. Call your local emergency number directly.')
    ensure_user_record(user_id)

    contacts = DatabaseService.get_emergency_contacts(user_id)
    if not contacts:
        return jsonify({'error': 'No emergency contacts', 'sent': 0}), 400

    location = None
    try:
        lat, lon = float(data.get('lat')), float(data.get('lon'))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            location = f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"
    except (TypeError, ValueError):
        pass

    name = get_user_display_name(user_id)
    when = datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')
    text = (f"🆘 SOS from {name}\n\n"
            f"They pressed the SOS button in Affinia at {when} and asked you, as their emergency contact, "
            f"to get in touch right now.")
    if location:
        text += f"\n\nLast known location: {location}"
    text += "\n\nIf you cannot reach them, consider calling local emergency services."

    sent = 0
    for contact_id in contacts:
        try:
            run_bot_coro(application.bot.send_message(chat_id=int(contact_id), text=text))
            sent += 1
        except Exception as e:  # a contact may have blocked the bot; keep going
            logging.warning(f"🆘 SOS message to {utag(contact_id)} failed: {e}")
        notify(contact_id, 'sos', user_id, f"{name} sent an SOS alert")

    logging.warning(f"🆘 SOS by {utag(user_id)}: {sent}/{len(contacts)} contacts alerted, location={'yes' if location else 'no'}")
    return jsonify({'sent': sent, 'contacts': len(contacts)})


@bp.route('/get_missions', methods=['POST'])
def get_missions():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    ensure_user_record(user_id)
    return jsonify({
        'completed': DatabaseService.get_completed_missions(user_id),
        'rewards': MISSIONS,
    })


@bp.route('/complete_mission', methods=['POST'])
def complete_mission():
    data = request.json or {}

    validated_user = get_validated_user(data)
    if not validated_user:
        return jsonify({'error': 'Invalid authentication'}), 401

    user_id = validated_user['user_id']
    mission_id = str(data.get('mission_id', ''))
    if mission_id not in MISSIONS:
        return jsonify({'error': 'Unknown mission'}), 400
    ensure_user_record(user_id)

    allowed, why = _mission_allowed(user_id, mission_id)
    if not allowed:
        return jsonify({'error': why}), 400

    reward = MISSIONS[mission_id]
    new_balance = DatabaseService.complete_mission(user_id, mission_id, reward)
    if new_balance is None:
        return jsonify({'error': 'Mission already completed', 'already_completed': True}), 400

    with data_lock:
        user_tokens[user_id] = new_balance
    logging.info(f"🏁 {utag(user_id)} completed mission {mission_id} (+{reward})")
    return jsonify({'success': True, 'mission_id': mission_id, 'reward': reward, 'balance': new_balance,
                    'completed': DatabaseService.get_completed_missions(user_id)})
