"""Time-limited consents (auto-expiry), optional per-record notes, and renewal after expiry."""
from datetime import timedelta

from sqlalchemy import update

from conftest import post
from services.database_service import Consent, utcnow
from test_persistence import reload_cache


def _backdate_expiry(app_module, consent_id: int, minutes_ago: int = 5) -> None:
    db = app_module.DatabaseService
    session = db.get_session()
    try:
        session.execute(
            update(Consent)
            .where(Consent.id == consent_id)
            .values(expires_at=utcnow() - timedelta(minutes=minutes_ago))
        )
        session.commit()
    finally:
        db.close_session()


def test_consent_options_validation(client):
    user = 8101
    post(client, '/get_balance', user)

    for bad_hours in (-1, True, '24', 99999):
        r = post(client, '/send_consent_invite', user, duration_hours=bad_hours)
        assert r.status_code == 400, bad_hours

    assert post(client, '/send_consent_invite', user, note='x' * 201).status_code == 400
    assert post(client, '/send_consent_invite', user, note=123).status_code == 400

    # 0 or empty string means ongoing (None)
    ok = post(client, '/create_consent_link', user, duration_hours=0, note='   ').get_json()
    assert ok['duration_hours'] is None and ok['note'] is None


def test_time_limited_consent_exposes_terms_auto_expires_and_allows_renewal(client, app_module):
    a, b = 8201, 8202
    post(client, '/get_balance', a)
    post(client, '/get_balance', b)

    inv = post(client, '/send_consent_invite', a, duration_hours=24, note='  Lisbon\nfestival  ').get_json()
    assert inv['duration_hours'] == 24
    assert inv['note'] == 'Lisbon festival'
    assert '24 hours' in inv['message']
    token = inv['invite_link'].split('start=')[1]

    # Before accepting, the recipient sees the proposed duration and note via /invite_info
    info = post(client, '/invite_info', b, invite_id=token).get_json()
    assert info['duration_hours'] == 24
    assert info['note'] == 'Lisbon festival'

    acc = post(client, '/handle_consent_invite', b, invite_id=token, decision='accept').get_json()
    assert acc['success'] and acc['tokens_earned'] == 100 and acc['consent_recorded'] is True

    beloved_now = post(client, '/get_beloved', a).get_json()['beloved'][0]
    assert beloved_now['relationship_status'] == 'current'
    assert beloved_now['active_duration_hours'] == 24
    assert beloved_now['active_note'] == 'Lisbon festival'
    assert beloved_now['active_expires_at'] > 0
    consent_id = beloved_now['active_consent_id']

    # Survives dyno restart before expiry
    reload_cache(app_module)
    hist = post(client, '/get_consent_history', b).get_json()
    assert hist['active_consents'] == 1 and hist['expired_consents'] == 0
    assert hist['consent_history'][0]['note'] == 'Lisbon festival'
    assert hist['consent_history'][0]['expires_at'] > hist['consent_history'][0]['timestamp']

    # Simulate expiry passing
    _backdate_expiry(app_module, consent_id, minutes_ago=10)

    beloved_after = post(client, '/get_beloved', a).get_json()['beloved'][0]
    assert beloved_after['relationship_status'] == 'past'
    assert beloved_after['active_consent_id'] is None
    assert beloved_after['last_consent_status'] == 'expired'

    hist_after = post(client, '/get_consent_history', b).get_json()
    assert hist_after['active_consents'] == 0 and hist_after['expired_consents'] == 1
    assert hist_after['consent_history'][0]['status'] == 'expired'

    # Revoking an already-expired consent does not overwrite 'expired' with 'revoked'
    assert post(client, '/revoke_consent', a, consent_id=consent_id).get_json()['success']
    reload_cache(app_module)
    assert post(client, '/get_consent_history', a).get_json()['consent_history'][0]['status'] == 'expired'

    # Next weekend: a fresh consent link between the same pair records a new active consent,
    # moves them back to 'current', and does NOT pay +100 AFN a second time.
    token2 = post(client, '/generate_consent_qr', b, duration_hours=12, note='Round two').get_json()['consent_link'].split('start=')[1]
    acc2 = post(client, '/handle_consent_invite', a, invite_id=token2, decision='accept').get_json()
    assert acc2['success'] and acc2['tokens_earned'] == 0 and acc2['consent_recorded'] is True
    assert app_module.user_tokens['8201'] == 100 and app_module.user_tokens['8202'] == 100

    beloved_renewed = post(client, '/get_beloved', a).get_json()['beloved'][0]
    assert beloved_renewed['relationship_status'] == 'current'
    assert beloved_renewed['active_duration_hours'] == 12
    assert beloved_renewed['active_note'] == 'Round two'
    assert post(client, '/get_consent_history', a).get_json()['total_consents'] == 2


def test_export_and_erasure_handle_notes_and_expiry(client, app_module):
    me, partner = 8301, 8302
    post(client, '/get_balance', me)
    post(client, '/get_balance', partner)

    token = post(client, '/send_consent_invite', me, duration_hours=72, note='Private trip note').get_json()['invite_link'].split('start=')[1]
    post(client, '/handle_consent_invite', partner, invite_id=token, decision='accept')

    exported = post(client, '/export_my_data', me).get_json()
    assert exported['consents'][0]['duration_hours'] == 72
    assert exported['consents'][0]['note'] == 'Private trip note'
    assert exported['consents'][0]['expires_at'] is not None

    # Deleting one user's account clears the shared note as well as revoking the consent
    assert post(client, '/delete_account', me, confirm='DELETE').get_json()['success']
    partner_export = app_module.DatabaseService.export_user('8302')
    assert partner_export['consents'][0]['status'] == 'revoked'
    assert partner_export['consents'][0]['note'] is None


def test_delete_beloved_withdraws_active_consent_and_blocks_reward_farming(client, app_module):
    a, b = 8401, 8402
    post(client, '/get_balance', a)
    post(client, '/get_balance', b)

    t1 = post(client, '/send_consent_invite', a).get_json()['invite_link'].split('start=')[1]
    post(client, '/handle_consent_invite', b, invite_id=t1, decision='accept')
    assert app_module.user_tokens['8401'] == 100

    # Removing the partner withdraws active consent, as promised by the UI
    assert post(client, '/delete_beloved', a, beloved_id='8402').get_json()['success']
    assert post(client, '/get_consent_history', b).get_json()['consent_history'][0]['status'] == 'revoked'

    # Re-connecting later creates a new consent without paying +100 AFN again
    t2 = post(client, '/send_consent_invite', a).get_json()['invite_link'].split('start=')[1]
    r2 = post(client, '/handle_consent_invite', b, invite_id=t2, decision='accept').get_json()
    assert r2['tokens_earned'] == 0 and r2['consent_recorded'] is True
    assert app_module.user_tokens['8401'] == 100 and app_module.user_tokens['8402'] == 100
