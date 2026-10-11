"""Flask application factory and blueprints."""
from flask import Flask, jsonify, request

from ..config import IP_RATE_LIMIT, PROXY_HOPS, ROOT_DIR
from ..security import RateLimited, enforce_rate_limit


def create_app() -> Flask:
    # static_folder=None: /static/* is served by an explicit route in pages.py (mime types, manifest)
    app = Flask('affinia', root_path=ROOT_DIR, template_folder='templates', static_folder=None)

    # За Heroku-роутером реальный IP клиента — последний элемент X-Forwarded-For (роутер дописывает его сам,
    # всё что левее — прислал клиент и может быть подделано). PROXY_HOPS=0 для запуска без прокси.
    if PROXY_HOPS > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS, x_proto=1, x_host=1)

    @app.errorhandler(RateLimited)
    def _handle_rate_limited(e):
        return jsonify({'error': e.message}), 429

    @app.before_request
    def _ip_rate_limit():
        if request.method == 'POST' and request.remote_addr:
            enforce_rate_limit(f'ip:{request.remote_addr}', *IP_RATE_LIMIT)

    from .account import bp as account_bp
    from .pages import bp as pages_bp
    from .payments import bp as payments_bp
    from .relations import bp as relations_bp
    from .safety import bp as safety_bp
    for bp in (pages_bp, account_bp, relations_bp, payments_bp, safety_bp):
        app.register_blueprint(bp)
    return app



