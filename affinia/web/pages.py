"""HTML pages, static files and manifests."""
import os

from flask import Blueprint, current_app, jsonify, render_template, send_from_directory

from ..config import BOT_USERNAME, POLICY_UPDATED, PRIVACY_CONTACT, TGA_APP_NAME, TGA_TOKEN, WEBAPP_URL

bp = Blueprint('pages', __name__)


@bp.route('/mini_app')
def mini_app():
    return render_template(
        'index.html',
        analytics_token=TGA_TOKEN,
        analytics_app_name=TGA_APP_NAME,
        bot_username=BOT_USERNAME,
        tg_startapp_link=f"https://t.me/{BOT_USERNAME}?startapp"
    )


@bp.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(current_app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')


@bp.route('/static/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/json')


@bp.route('/static/<path:filename>')
def static_files(filename):
    # Let Flask infer the correct mime type for static assets
    return send_from_directory('static', filename)


@bp.route('/')
def index():
    return render_template(
        'index.html',
        analytics_token=TGA_TOKEN,
        analytics_app_name=TGA_APP_NAME,
        bot_username=BOT_USERNAME,
        tg_startapp_link=f"https://t.me/{BOT_USERNAME}?startapp"
    )


@bp.route('/tonconnect-manifest.json')
def tonconnect_manifest():
    # TON Connect manifest for wallet integration
    manifest = {
        "url": WEBAPP_URL,
        "name": "Affinia",
        "iconUrl": f"{WEBAPP_URL}/static/icon-192x192.png"
    }
    return jsonify(manifest)


@bp.route('/test-manifest')
def test_manifest():
    return "Manifest test works! Version 2.0"


@bp.route('/privacy')
def privacy_policy():
    return render_template('privacy.html', bot_username=BOT_USERNAME, contact=PRIVACY_CONTACT, updated=POLICY_UPDATED)


@bp.route('/terms')
def terms_of_service():
    return render_template('terms.html', bot_username=BOT_USERNAME, contact=PRIVACY_CONTACT, updated=POLICY_UPDATED)
