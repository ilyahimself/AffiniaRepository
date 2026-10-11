# Affinia

Telegram Mini App for recording **mutual consent** between two adults, with a small trust network of friends and partners.

Both people have to accept. Either one can revoke at any time. The record is a timestamped row in a database that both parties can see тАФ not a contract, not a certificate, not legal advice. Consent in real life can be withdrawn at any moment regardless of what the app shows; Affinia only helps people make the agreement explicit and gives them a way to document it.

- Bot: [@AffiniaBot](https://t.me/AffiniaBot)
- Privacy Policy: `/privacy` ┬╖ Terms: `/terms` (served by the app)
- Status: early access, ~400 registered Telegram users, production on Heroku (US region)

## What it does today

| Feature | How it works |
|---|---|
| Consent invitation | User asks the server for a link/QR. The server issues a random single-use token (7-day TTL) stored in `invites`. The other person opens it in Telegram, sees who invited them (name + avatar), and accepts or declines. |
| Consent record | On acceptance a row is written to `consents` (both IDs, time, status). Visible to both. Either side can revoke; the row is kept with `revoked_at` / `revoked_by`. |
| Friend network | Same invitation mechanism with a 30-day TTL; links are stored in `referral_friends`. |
| Emergency contacts & SOS | A user marks friends as emergency contacts (`referral_friends.is_emergency`). The SOS button opens the phone dialer with 112 and calls `/sos`, which makes the bot message each emergency contact with the time and, if the user allowed it, a map link. Rate-limited to 5 per hour. |
| Learn & Earn | Six missions (three short reads on consent/safety, friend milestones, supporter badge). Completion and rewards are recorded server-side in `mission_completions`, once per user. |
| AFN points | Internal points for inviting friends (+25), partners (+100) and missions, tracked in `ledger`. **Not a token, not a currency, not transferable.** |
| Telegram Stars | Voluntary support of the project via Telegram's native payments; recorded idempotently by Telegram's charge id. Donations do not unlock features. |
| Data rights | Export everything stored about you as JSON; delete your account (immediate, irreversible, keeps the other party's record consistent). |

Everything in the UI is backed by the server. Earlier placeholder screens (phone/email/ID verification, blockchain verification, witness mode, nearby search, TON wallet, partner search/messaging) were removed in October 2026 rather than left as "coming soon".

## Architecture

```
Telegram client тФАтФАтЦ║ Mini App (templates/index.html, single page)
       тФВ                    тФВ  POST JSON + Telegram initData (HMAC-signed, max age 24h)
       тЦ╝                    тЦ╝
 python-telegram-bot тЧДтФАтЦ║ Flask (app.py) тФАтФАтЦ║ SQLAlchemy тФАтФАтЦ║ PostgreSQL (Heroku)
   (long polling,          one gunicorn
    same process)          worker, 8 threads
```

- **Auth**: every API call carries Telegram `initData`; the server verifies the HMAC with the bot token and rejects stale signatures. No sessions, no passwords.
- **Storage**: PostgreSQL is the only source of truth; an in-memory read cache is rebuilt on start. Schema managed by Alembic (`migrations/`), applied in the Heroku release phase.
- **Rate limiting**: in-memory sliding windows per IP, per user, and stricter buckets for failed auth, invite creation/lookup and payments.
- **Logs**: user identifiers are replaced by a keyed hash (`u:тАж`); names and Telegram IDs are never logged.
- **Secrets**: only in environment variables. Rotated credentials; git history was rewritten to purge earlier leaks (October 2026).

Tables: `users`, `referral_friends`, `beloved_partners`, `consents`, `invites`, `notifications`, `payments`, `ledger`.

## Running locally

Requirements: Python 3.12, PostgreSQL (or SQLite for a quick start).

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # fill in API_TOKEN, BOT_USERNAME, WEBAPP_URL, DATABASE_URL
alembic upgrade head
python app.py                                      # Flask dev server + bot polling (dev only)
```

For the Mini App to open inside Telegram you need a public HTTPS URL (e.g. an ngrok tunnel) set both in `WEBAPP_URL` and in @BotFather тЖТ Bot Settings тЖТ Menu Button / Mini App.

Production uses `Procfile`: `release: alembic upgrade head`, `web: gunicorn app:app -c gunicorn.conf.py`. The worker count must stay at 1 because the bot's long polling lives in the same process.

## Tests

```bash
pytest tests -q
```

Tests run against a temporary SQLite database with a fake bot token and forge valid `initData` signatures. They cover authentication and freshness, invite forgery/reuse/expiry, reward idempotency, consent revocation, payment idempotency, rate limits, data export and erasure, and cache rebuild after a restart. CI (`.github/workflows/ci.yml`) runs them on every push, applies migrations to an empty database, and fails if something that looks like a bot token or a database URL with credentials is committed.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `API_TOKEN` | yes | Telegram bot token from @BotFather |
| `BOT_USERNAME` | yes | Bot username without `@` |
| `WEBAPP_URL` | yes | Public HTTPS base URL of the app |
| `DATABASE_URL` / `HEROKU_POSTGRESQL_COBALT_URL` | yes | SQLAlchemy URL; `postgres://` is rewritten to `postgresql://` |
| `LOG_SALT` | no | Salt for pseudonymous user tags in logs (defaults to the bot token) |
| `INIT_DATA_MAX_AGE` | no | Max age of Telegram `initData` in seconds (default 86400) |
| `PROXY_HOPS` | no | Trusted reverse-proxy hops for client IP (auto: 1 on Heroku, 0 elsewhere) |
| `TGA_TOKEN`, `TGA_APP_NAME` | no | Telegram Apps Center analytics; the SDK is only embedded when both are set |
| `PRIVACY_CONTACT` | no | Email shown on `/privacy` and `/terms` |

## Repository layout

```
app.py                  Entry point; builds the Flask app from the affinia/ package
affinia/                config, security (initData + rate limits), state cache, users, invites
affinia/bot/            bot runtime (polling/webhook) and command handlers
affinia/web/            Flask blueprints: pages, account, relations, payments
services/database_service.py   SQLAlchemy models and all DB access
migrations/             Alembic
templates/index.html    the Mini App (single page)
templates/privacy.html, terms.html
static/                 icons, manifest
tests/                  pytest suite
gunicorn.conf.py, Procfile, alembic.ini
scripts/update_icons.py generate favicon/app icons from one image
```

## Roadmap (short)

1. ~~Split `app.py` into modules~~ done: `affinia/` package (config, security, state, users, invites, bot/, web/ blueprints).
2. ~~Remove placeholder screens~~ done (October 2026).
3. ~~Real SOS~~ done: emergency contacts + bot alert + 112 dialer. Still to verify on iOS/Android inside Telegram.
4. Time-limited consents (auto-expire), optional per-record notes.
5. Escape user-provided names in the Mini App templates (they come from Telegram profiles and are inserted via innerHTML).
6. EU hosting region before public launch.

## Legal

Privacy Policy and Terms are served by the app at `/privacy` and `/terms`. They are drafts prepared for legal review; the operating entity will be stated once incorporated. The service is for adults (18+).

## Licence

All rights reserved for now; a licence will be chosen before the public launch.
