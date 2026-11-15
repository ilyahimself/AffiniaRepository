# Security

## Reporting

If you find a vulnerability, please message the project via the bot [@AffiniaBot](https://t.me/AffiniaBot) or the contact on `/privacy`. Do not open a public issue for security problems. We aim to acknowledge within 72 hours.

## What is in place

- **Authentication**: every API request carries Telegram `initData`, verified with HMAC-SHA256 against the bot token (`validate_init_data` in `app.py`). Signatures older than 24 h or from the future are rejected. There are no passwords or sessions to steal.
- **Invitations**: random single-use tokens issued by the server (`secrets.token_urlsafe(16)`), validated against the `invites` table with TTL, kind and self-accept checks. The inviter is read from the database, never from the link.
- **Rewards and payments**: idempotent (`ledger` with reason/ref; `payments` unique on Telegram charge id).
- **Rate limiting**: per IP (behind the Heroku router via `ProxyFix`, last `X-Forwarded-For` hop), per user, and strict buckets for failed auth, invite creation/lookup, data export and donations.
- **Secrets**: environment variables only. The bot token and database credentials were rotated and the git history rewritten in October 2026 after earlier leaks. CI fails on anything shaped like a bot token or a database URL with credentials.
- **Logs**: no Telegram IDs or names; a keyed hash per user instead.
- **Dependencies**: pinned in `requirements.txt`; updated October 2026 (python-telegram-bot 22, Pillow 12, SQLAlchemy 2.0.x).
- **Third-party scripts**: only the Telegram WebApp SDK and (when configured) the Telegram Apps Center analytics SDK, loaded `async`. The TON Connect wallet widget was removed in October 2026.
- **SOS**: `/sos` is rate-limited per user (5/hour); messages go only to friends the user explicitly marked as emergency contacts. Location is sent only if the browser granted it for that request.
- **Data rights & accountability**: unbundled 18+ & GDPR art. 9(2)(a) onboarding gate stored with UTC timestamps (`/accept_compliance`), plus self-service export and deletion (`/export_my_data`, `/delete_account`).

- **Browser headers**: HTML responses carry a `Content-Security-Policy` restricting `connect-src` and `script-src` to Telegram (and optional analytics) origins, `Referrer-Policy: no-referrer` (so `?invite=` tokens never leak in `Referer` headers), and `X-Content-Type-Options: nosniff`. All user-supplied strings in the Mini App are escaped via `escapeHtml()`.

## Known limitations

- Rate limiting is in-memory and resets on restart; fine for one dyno, would need Redis for more.
- Hosting is in the US region (Heroku); see the Privacy Policy for the transfer basis.
- `initData` is sent in the JSON body of each request; TLS protects it in transit, and CSP restricts `connect-src` exfiltration, though moving inline scripts into static files would allow dropping `'unsafe-inline'` in the future.

## Incident response

1. Revoke the bot token in @BotFather and set the new one: `heroku config:set API_TOKEN=…`.
2. Rotate database credentials: `heroku pg:credentials:rotate HEROKU_POSTGRESQL_COBALT_URL -a affinia`.
3. Check `heroku logs` for the pseudonymous tags involved; `LOG_SALT` lets you re-derive a tag from a known user id.
4. Notify affected users via the bot if personal data was exposed (GDPR art. 33/34 timelines apply).
