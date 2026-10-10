"""Shared fixtures: an isolated sqlite database and the Flask app with a fake bot token."""
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import urlencode

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAKE_BOT_TOKEN = '123456789:TESTTOKENxxxxxxxxxxxxxxxxxxxxxxxxxx'


@pytest.fixture(scope='session')
def app_module(tmp_path_factory):
    db_path = tmp_path_factory.mktemp('db') / 'affinia_test.db'
    # Both vars point at sqlite so that load_dotenv() in app.py can never
    # substitute the production URL from .env (dotenv does not override existing vars).
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path.as_posix()}'
    os.environ['HEROKU_POSTGRESQL_COBALT_URL'] = os.environ['DATABASE_URL']
    os.environ['API_TOKEN'] = FAKE_BOT_TOKEN
    os.environ['BOT_USERNAME'] = 'TestBot'
    os.environ['WEBAPP_URL'] = 'http://localhost:5000'

    import app as app_module
    from services.database_service import DatabaseService

    DatabaseService.initialize(os.environ['DATABASE_URL'], create_tables=True)
    app_module.load_data()
    return app_module


@pytest.fixture
def client(app_module):
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def make_init_data(user_id: int, token: str = FAKE_BOT_TOKEN, username: str = '', auth_date: int = 1_700_000_000) -> str:
    """Build a Telegram WebApp initData string with a valid hash for `token`."""
    user = {'id': user_id, 'first_name': f'User{user_id}'}
    if username:
        user['username'] = username
    fields = {'auth_date': str(auth_date), 'query_id': 'AAE', 'user': json.dumps(user, separators=(',', ':'))}
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def post(client, path: str, user_id: int, **body):
    body['init_data'] = make_init_data(user_id)
    return client.post(path, json=body)
