"""Every mutation must land in the database and survive a cache reload (simulated restart)."""
from conftest import make_init_data, post


def reload_cache(app_module):
    """Simulate a dyno restart: drop the in-memory cache and rebuild it from the DB."""
    app_module.user_tokens.clear()
    app_module.referrals.clear()
    app_module.beloveds.clear()
    app_module.consent_history.clear()
    app_module.user_notifications.clear()
    app_module.new_friends_count.clear()
    app_module.new_partners_count.clear()
    app_module.load_data()


def test_unauthenticated_requests_are_rejected(client):
    assert client.post('/get_balance', json={'user_id': '1'}).status_code == 401
    bad = make_init_data(1, token='999:WRONGTOKENxxxxxxxxxxxxxxxxxxxxxxxxxxx')
    assert client.post('/get_balance', json={'init_data': bad}).status_code == 401


def test_balance_endpoint_creates_user_in_db(client, app_module):
    r = post(client, '/get_balance', 1001)
    assert r.status_code == 200 and r.get_json()['balance'] == 0
    assert app_module.DatabaseService.get_user('1001') is not None


def test_consent_flow_persists_and_rewards_once(client, app_module):
    inviter, partner = 2001, 2002
    post(client, '/get_balance', inviter)
    post(client, '/get_balance', partner)

    invite = post(client, '/send_consent_invite', inviter).get_json()
    invite_id = invite['invite_link'].split('start=')[1]
    assert app_module.DatabaseService.get_invite(invite_id) is not None

    r = post(client, '/handle_consent_invite', partner, invite_id=invite_id, decision='accept').get_json()
    assert r['success'] and r['tokens_earned'] == 100

    # Second accept of the same invite must not pay again or duplicate the consent
    r2 = post(client, '/handle_consent_invite', partner, invite_id=invite_id, decision='accept').get_json()
    assert r2['success'] and r2['tokens_earned'] == 0

    reload_cache(app_module)

    assert app_module.user_tokens['2001'] == 100
    assert app_module.user_tokens['2002'] == 100
    assert '2002' in app_module.beloveds['2001'] and '2001' in app_module.beloveds['2002']
    assert len(app_module.consent_history['2001']) == 1
    assert app_module.consent_history['2001'][0]['status'] == 'active'

    beloved = post(client, '/get_beloved', inviter).get_json()['beloved']
    assert beloved[0]['relationship_status'] == 'current'
    assert beloved[0]['active_consent_id'] is not None
    assert beloved[0]['added_date']  # real timestamp, not "now"

    notifications = post(client, '/get_notifications', inviter).get_json()['notifications']
    assert [n['type'] for n in notifications] == ['consent_accepted']
    # Delivered once
    assert post(client, '/get_notifications', inviter).get_json()['notifications'] == []


def test_revoke_consent_changes_status_for_both_sides(client, app_module):
    a, b = 3001, 3002
    post(client, '/get_balance', a)
    post(client, '/get_balance', b)
    invite_id = post(client, '/send_consent_invite', a).get_json()['invite_link'].split('start=')[1]
    post(client, '/handle_consent_invite', b, invite_id=invite_id, decision='accept')

    consent_id = app_module.consent_history['3001'][0]['id']

    # A stranger cannot revoke it
    assert post(client, '/revoke_consent', 3999, consent_id=consent_id).status_code == 404

    assert post(client, '/revoke_consent', b, consent_id=consent_id).get_json()['success']

    reload_cache(app_module)
    assert app_module.consent_history['3001'][0]['status'] == 'revoked'
    assert app_module.consent_history['3002'][0]['status'] == 'revoked'

    beloved = post(client, '/get_beloved', a).get_json()['beloved']
    assert beloved[0]['relationship_status'] == 'past'
    assert beloved[0]['active_consent_id'] is None


def test_friend_flow_persists_and_counters_survive_restart(client, app_module):
    inviter, invitee = 4001, 4002
    post(client, '/get_balance', inviter)
    post(client, '/get_balance', invitee)

    invite_id = post(client, '/send_friend_invite', inviter).get_json()['invite_link'].split('start=')[1]
    r = post(client, '/handle_friend_invite', invitee, invite_id=invite_id, decision='accept').get_json()
    assert r['tokens_earned'] == 25

    reload_cache(app_module)
    assert app_module.user_tokens['4001'] == 50
    assert app_module.user_tokens['4002'] == 25
    assert '4002' in app_module.referrals['4001']
    assert post(client, '/get_new_friends_count', inviter).get_json()['new_friends_count'] == 1

    post(client, '/clear_new_friends', inviter)
    reload_cache(app_module)
    assert post(client, '/get_new_friends_count', inviter).get_json()['new_friends_count'] == 0

    assert post(client, '/delete_friend', inviter, friend_id='4002').get_json()['success']
    reload_cache(app_module)
    assert '4002' not in app_module.referrals.get('4001', [])


def test_invite_from_unknown_or_self_is_rejected(client):
    post(client, '/get_balance', 5001)
    r = post(client, '/handle_consent_invite', 5001, invite_id='consent_5001_1', decision='accept')
    assert r.status_code == 400
    r = post(client, '/handle_consent_invite', 5001, invite_id='consent_999999_1', decision='accept')
    assert r.status_code == 400


def test_payment_is_recorded_once(app_module):
    db = app_module.DatabaseService
    db.ensure_user('6001')
    assert db.record_payment('charge-A', '6001', 150) == 150
    assert db.record_payment('charge-A', '6001', 150) is None  # duplicate webhook delivery
    assert db.record_payment('charge-B', '6001', 50) == 200
