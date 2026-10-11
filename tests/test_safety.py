"""Emergency contacts, SOS alerts and server-side missions."""
from conftest import post


def _befriend(client, a, b):
    post(client, '/get_balance', a)
    post(client, '/get_balance', b)
    invite_id = post(client, '/send_friend_invite', a).get_json()['invite_link'].split('start=')[1]
    assert post(client, '/handle_friend_invite', b, invite_id=invite_id, decision='accept').status_code == 200


def test_emergency_contact_requires_friendship_and_is_one_directional(client, app_module):
    a, b, stranger = 9101, 9102, 9103
    _befriend(client, a, b)
    post(client, '/get_balance', stranger)

    assert post(client, '/set_emergency_contact', a, friend_id=str(stranger)).status_code == 400

    r = post(client, '/set_emergency_contact', a, friend_id=str(b)).get_json()
    assert r['success'] and r['emergency_contacts'] == [str(b)]

    friends_of_a = post(client, '/get_friends', a).get_json()['friends']
    assert friends_of_a == [{'user_id': str(b), 'trust_level': 'emergency'}]
    # b did not mark a, so from b's side a is a plain friend; and no balances are exposed
    friends_of_b = post(client, '/get_friends', b).get_json()['friends']
    assert friends_of_b == [{'user_id': str(a), 'trust_level': 'friend'}]

    assert post(client, '/set_emergency_contact', a, friend_id=str(b), enabled=False).get_json()['emergency_contacts'] == []


def test_sos_alerts_each_emergency_contact_once(client, app_module, monkeypatch):
    import affinia.web.safety as safety

    a, b, c = 9201, 9202, 9203
    _befriend(client, a, b)
    _befriend(client, a, c)

    r = post(client, '/sos', a)
    assert r.status_code == 400 and r.get_json()['sent'] == 0

    post(client, '/set_emergency_contact', a, friend_id=str(b))
    post(client, '/set_emergency_contact', a, friend_id=str(c))

    sent = []

    def fake_run(coro, *args, **kwargs):
        sent.append(coro.cr_frame.f_locals.get('chat_id'))
        coro.close()

    monkeypatch.setattr(safety, 'run_bot_coro', fake_run)
    r = post(client, '/sos', a, lat=38.7223, lon=-9.1393).get_json()
    assert r == {'sent': 2, 'contacts': 2}
    assert sorted(sent) == [b, c]

    # Contacts also get an in-app notification
    notes = post(client, '/get_notifications', b).get_json()['notifications']
    assert any(n['type'] == 'sos' for n in notes)


def test_sos_is_rate_limited(client, app_module, monkeypatch):
    import affinia.web.safety as safety

    a, b = 9301, 9302
    _befriend(client, a, b)
    post(client, '/set_emergency_contact', a, friend_id=str(b))
    monkeypatch.setattr(safety, 'run_bot_coro', lambda coro, *a, **k: coro.close())

    limit, _ = safety.SOS_LIMIT
    for _ in range(limit):
        assert post(client, '/sos', a).status_code == 200
    assert post(client, '/sos', a).status_code == 429


def test_missions_reward_once_and_check_eligibility(client, app_module):
    uid = 9401
    before = post(client, '/get_balance', uid).get_json()['balance']
    assert post(client, '/get_missions', uid).get_json()['completed'] == []

    r = post(client, '/complete_mission', uid, mission_id='consent_basics').get_json()
    assert r['success'] and r['reward'] == 15 and r['balance'] == before + 15

    r = post(client, '/complete_mission', uid, mission_id='consent_basics')
    assert r.status_code == 400 and r.get_json()['already_completed']
    assert post(client, '/get_balance', uid).get_json()['balance'] == before + 15

    # Needs a friend first
    assert post(client, '/complete_mission', uid, mission_id='first_referral').status_code == 400
    _befriend(client, uid, 9402)
    assert post(client, '/complete_mission', uid, mission_id='first_referral').get_json()['reward'] == 30

    # No donation recorded -> not allowed; unknown ids rejected
    assert post(client, '/complete_mission', uid, mission_id='first_donation').status_code == 400
    assert post(client, '/complete_mission', uid, mission_id='phone_verify').status_code == 400

    assert sorted(post(client, '/get_missions', uid).get_json()['completed']) == ['consent_basics', 'first_referral']
