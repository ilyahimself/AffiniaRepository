"""Right of access / erasure endpoints, legal pages, and log pseudonymisation (H5)."""
import logging

from conftest import post


def _connect_consent(client, inviter, partner):
    token = post(client, '/send_consent_invite', inviter).get_json()['invite_link'].split('start=')[1]
    r = post(client, '/handle_consent_invite', partner, invite_id=token, decision='accept').get_json()
    assert r['success']


def test_legal_pages_render(client):
    for path, marker in (('/privacy', 'Privacy Policy'), ('/terms', 'Terms of Service')):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.get_data(as_text=True)
    body = client.get('/privacy').get_data(as_text=True)
    assert 'United States' in body and 'not</strong> end-to-end' in body


def test_export_contains_everything_about_the_user(client, app_module):
    me, partner = 9001, 9002
    post(client, '/get_balance', me)
    post(client, '/get_balance', partner)
    _connect_consent(client, me, partner)

    r = post(client, '/export_my_data', me)
    assert r.status_code == 200
    data = r.get_json()
    assert data['user']['user_id'] == '9001'
    assert data['partners'] == ['9002']
    assert len(data['consents']) == 1 and data['consents'][0]['status'] == 'active'
    assert any(e['reason'] for e in data['ledger'])
    assert 'exported_at' in data

    assert client.post('/export_my_data', json={'user_id': '9001'}).status_code == 401


def test_delete_account_erases_user_and_keeps_other_party_consistent(client, app_module):
    me, partner, friend = 9101, 9102, 9103
    for u in (me, partner, friend):
        post(client, '/get_balance', u)
    _connect_consent(client, me, partner)
    ftoken = post(client, '/send_friend_invite', me).get_json()['invite_link'].split('start=')[1]
    assert post(client, '/handle_friend_invite', friend, invite_id=ftoken, decision='accept').get_json()['success']
    post(client, '/send_consent_invite', me)  # an unused invite that must disappear too

    # Confirmation word is required
    assert post(client, '/delete_account', me).status_code == 400
    assert post(client, '/delete_account', me, confirm='yes').status_code == 400

    assert post(client, '/delete_account', me, confirm='DELETE').get_json()['success']

    DB = app_module.DatabaseService
    assert DB.get_user('9101') is None
    assert DB.export_user('9101') is None
    assert '9101' not in app_module.user_tokens
    assert '9101' not in app_module.beloveds.get('9102', [])
    assert '9101' not in app_module.referrals.get('9103', [])
    assert not any(inv.inviter_id == '9101' for inv in [DB.get_invite(ftoken)] if inv)

    # The partner still has a consent record, now revoked and pointing at the placeholder
    partner_export = DB.export_user('9102')
    assert len(partner_export['consents']) == 1
    c = partner_export['consents'][0]
    assert c['status'] == 'revoked' and c['revoked_by'] == 'deleted'
    assert '9101' not in (c['inviter_id'], c['partner_id'])
    assert partner_export['partners'] == []

    # The placeholder user is never surfaced as a real user
    assert 'deleted' not in app_module.user_tokens

    # A deleted user who comes back starts from scratch
    r = post(client, '/get_balance', me)
    assert r.status_code == 200 and r.get_json()['balance'] == 0


def test_deleted_user_payments_are_kept_anonymised(client, app_module):
    me = 9201
    post(client, '/get_balance', me)
    DB = app_module.DatabaseService
    assert DB.record_payment('charge_del_1', '9201', 50) == 50
    assert post(client, '/delete_account', me, confirm='DELETE').get_json()['success']
    from sqlalchemy import select
    from services.database_service import Payment
    session = DB.get_session()
    try:
        p = session.execute(select(Payment).where(Payment.charge_id == 'charge_del_1')).scalar_one()
        assert p.user_id == 'deleted' and p.amount == 50
    finally:
        DB.close_session()


def test_logs_do_not_contain_raw_user_ids(client, app_module, caplog):
    a, b = 9301, 9302
    with caplog.at_level(logging.INFO):
        post(client, '/get_balance', a)
        post(client, '/get_balance', b)
        _connect_consent(client, a, b)
    text = '\n'.join(r.getMessage() for r in caplog.records if r.name == 'root')
    assert '9301' not in text and '9302' not in text
    assert 'u:' in text
    assert app_module.utag('9301') == app_module.utag('9301') != app_module.utag('9302')
