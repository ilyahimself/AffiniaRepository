"""initData freshness (H1) and rate limiting / client IP handling (H2)."""
import time

from conftest import make_init_data, post


def _reset_limits(app_module):
    app_module._rate_buckets.clear()


def test_stale_init_data_is_rejected(client, app_module):
    _reset_limits(app_module)
    uid = 8001
    fresh = make_init_data(uid)
    assert client.post('/get_balance', json={'init_data': fresh}).status_code == 200

    stale = make_init_data(uid, auth_date=int(time.time()) - app_module.INIT_DATA_MAX_AGE - 60)
    assert client.post('/get_balance', json={'init_data': stale}).status_code == 401

    # Just inside the window is still fine
    recent = make_init_data(uid, auth_date=int(time.time()) - app_module.INIT_DATA_MAX_AGE + 600)
    assert client.post('/get_balance', json={'init_data': recent}).status_code == 200

    far_future = make_init_data(uid, auth_date=int(time.time()) + app_module.CLOCK_SKEW + 60)
    assert client.post('/get_balance', json={'init_data': far_future}).status_code == 401

    # A signature without auth_date at all is not accepted either
    assert client.post('/get_balance', json={'init_data': make_init_data(uid, auth_date=None)}).status_code == 401


def test_names_with_percent_and_plus_survive_decoding(client, app_module):
    """M11: parse_qs already URL-decodes; a second unquote used to corrupt names like '100% Ann+Bob'."""
    _reset_limits(app_module)
    uid = 8002
    data = make_init_data(uid, first_name='100% Ann+Bob %41 Ко')
    assert client.post('/get_balance', json={'init_data': data}).status_code == 200
    assert app_module.user_profiles[str(uid)]['first_name'] == '100% Ann+Bob %41 Ко'


def test_failed_auth_attempts_are_throttled_per_ip(client, app_module):
    _reset_limits(app_module)
    limit, _ = app_module.AUTH_FAIL_LIMIT
    bad = make_init_data(1, token='999:WRONGTOKENxxxxxxxxxxxxxxxxxxxxxxxxxxx')
    for i in range(limit):
        # PROXY_HOPS=0 in tests: a client-supplied X-Forwarded-For must not let the attacker look like a new IP
        r = client.post('/get_balance', json={'init_data': bad}, headers={'X-Forwarded-For': f'10.0.0.{i}'})
        assert r.status_code == 401
    r = client.post('/get_balance', json={'init_data': bad}, headers={'X-Forwarded-For': '10.0.1.1'})
    assert r.status_code == 429

    # A legitimate user from the same IP is not affected by the auth-fail bucket
    assert post(client, '/get_balance', 8002).status_code == 200
    _reset_limits(app_module)


def test_invite_creation_is_rate_limited(client, app_module):
    _reset_limits(app_module)
    uid = 8003
    for _ in range(30):
        assert post(client, '/send_friend_invite', uid).status_code == 200
    r = post(client, '/send_friend_invite', uid)
    assert r.status_code == 429 and 'invitations' in r.get_json()['error']
    # Other users are unaffected
    assert post(client, '/send_friend_invite', 8004).status_code == 200
    _reset_limits(app_module)


def test_invite_lookup_is_rate_limited(client, app_module):
    _reset_limits(app_module)
    uid = 8005
    post(client, '/get_balance', uid)
    for _ in range(60):
        assert post(client, '/invite_info', uid, invite_id='friend_guess').status_code == 400
    assert post(client, '/invite_info', uid, invite_id='friend_guess').status_code == 429
    _reset_limits(app_module)


def test_proxy_fix_uses_last_forwarded_ip_when_enabled(app_module):
    """Behind Heroku the router appends the real client IP last; earlier entries are attacker-controlled."""
    from werkzeug.middleware.proxy_fix import ProxyFix
    from flask import Flask, request

    probe = Flask('probe')
    probe.wsgi_app = ProxyFix(probe.wsgi_app, x_for=1)

    @probe.route('/ip')
    def ip():
        return request.remote_addr

    c = probe.test_client()
    assert c.get('/ip', headers={'X-Forwarded-For': '1.1.1.1, 2.2.2.2'}).data == b'2.2.2.2'
    assert c.get('/ip', headers={'X-Forwarded-For': '1.1.1.1'}).data == b'1.1.1.1'
