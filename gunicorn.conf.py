"""Конфигурация gunicorn для Heroku: `web: gunicorn app:app`.

Бот (long polling) живёт в том же процессе, что и Flask, поэтому воркер ровно один:
второй воркер начал бы второй getUpdates и Telegram отвечал бы Conflict.
Параллельность HTTP-запросов обеспечивают треды.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = 1
threads = int(os.environ.get('WEB_THREADS', '8'))
worker_class = 'gthread'
timeout = 60            # запрос дольше минуты — воркер перезапускается
graceful_timeout = 20
keepalive = 5
loglevel = 'info'
accesslog = None        # access-лог не нужен: /get_notifications опрашивается каждые 15 с, приложение логирует события само
errorlog = '-'


def post_fork(server, worker):
    """Поднимаем кэш из Postgres и запускаем бота после форка, когда процесс воркера уже свой."""
    import app as affinia
    affinia.start_bot_in_background()
    server.log.info('Telegram bot thread started in worker %s', worker.pid)
