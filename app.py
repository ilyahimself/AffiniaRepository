"""Affinia entry point.

The code lives in the affinia/ package:

    affinia/config.py      environment, logging, constants
    affinia/security.py    initData validation, rate limiting, log pseudonyms
    affinia/state.py       in-memory cache mirrored from PostgreSQL
    affinia/users.py       user records, profiles, avatars
    affinia/invites.py     server-issued invitation tokens, QR
    affinia/bot/           python-telegram-bot application and handlers
    affinia/web/           Flask app factory and blueprints (pages, account, relations, payments)
    services/database_service.py   SQLAlchemy models and all database access

This module wires it together and keeps the names that gunicorn.conf.py, the tests and
`python app.py` rely on. Production runs `gunicorn app:app -c gunicorn.conf.py`.
"""
import logging
import os
import threading

from services.database_service import DatabaseService  # noqa: F401  re-exported for tests/scripts

from affinia.config import (  # noqa: F401
    ADMIN_ID, API_TOKEN, AUTH_FAIL_LIMIT, BOT_USERNAME, CLOCK_SKEW, INIT_DATA_MAX_AGE, USE_WEBHOOK, WEBAPP_URL,
)
from affinia.security import (  # noqa: F401
    RateLimited, _rate_buckets, enforce_rate_limit, get_validated_user, utag, validate_init_data,
)
from affinia.state import (  # noqa: F401
    beloveds, consent_history, data_lock, load_data, new_friends_count, new_partners_count,
    pending_consent_requests, pending_friend_requests, referrals, user_notifications, user_profiles,
    user_stars, user_tokens,
)
from affinia.bot.runtime import (  # noqa: F401
    application, bot, run_bot_coro, run_bot_polling, run_bot_webhook, start_bot_in_background,
)
from affinia.invites import issue_invite, lookup_invite  # noqa: F401
from affinia.users import ensure_user_record  # noqa: F401
from affinia.web import create_app

app = create_app()


def _run_dev_server() -> None:
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)


if __name__ == '__main__':
    # Local development only: Flask dev server + bot in the same process.
    import asyncio

    load_data()
    logging.info(f'🚀 Starting Affinia (USE_WEBHOOK: {USE_WEBHOOK})')

    if USE_WEBHOOK:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_bot_webhook())
        loop.close()
        _run_dev_server()  # receives webhook calls
    else:
        threading.Thread(target=_run_dev_server, daemon=True).start()
        try:
            run_bot_polling()  # blocks in the main thread
        except KeyboardInterrupt:
            print('Bot stopped by user (Ctrl+C)')
