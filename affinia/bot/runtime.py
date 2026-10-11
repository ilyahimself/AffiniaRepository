"""PTB application instance, its event loop, and how the bot process is started."""
import asyncio
import logging
import sys
import threading

from telegram import Bot
from telegram.ext import Application

from ..config import API_TOKEN, BOT_USERNAME
from ..state import load_data

bot_loop = None


async def _remember_bot_loop(_application) -> None:
    """post_init: запоминаем loop, в котором реально крутится бот (не полагаемся на то, какой loop выберет PTB)."""
    global bot_loop
    bot_loop = asyncio.get_running_loop()


try:
    bot = Bot(token=API_TOKEN)
    application = (Application.builder().token(API_TOKEN).connection_pool_size(50)
                   .post_init(_remember_bot_loop).build())
    logging.info(f'✅ Bot initialized successfully with username: @{BOT_USERNAME}')
except Exception as e:
    logging.error(f'❌ Failed to initialize bot: {e}')
    sys.exit(1)


def run_bot_coro(coro, timeout: float = 10, wait: bool = True):
    """Выполняет корутину Telegram Bot API в event loop бота.

    Из потока Flask — через run_coroutine_threadsafe; изнутри самого loop бота —
    планирует задачу (блокироваться там нельзя, иначе deadlock).
    """
    loop = bot_loop
    if loop is None or not loop.is_running():
        coro.close()
        raise RuntimeError('Bot event loop is not running')
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        return loop.create_task(coro)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout) if wait else future


def run_bot_polling():
    """Запуск бота в режиме polling. Блокирует текущий поток до остановки бота."""
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    
    from .handlers import setup_handlers
    setup_handlers()
    in_main_thread = threading.current_thread() is threading.main_thread()
    logging.info(f'🔄 Starting bot in POLLING mode ({"main" if in_main_thread else "background"} thread)...')
    # Обработчики сигналов можно ставить только из главного потока; под gunicorn
    # остановкой управляет сам gunicorn, поток бота daemon и умирает вместе с воркером.
    application.run_polling(**({} if in_main_thread else {'stop_signals': None}))


_bot_thread = None


def start_bot_in_background() -> threading.Thread:
    """Точка входа для gunicorn (см. gunicorn.conf.py): кэш из БД + бот в отдельном потоке того же процесса."""
    global _bot_thread
    if _bot_thread is not None:
        return _bot_thread
    load_data()
    _bot_thread = threading.Thread(target=run_bot_polling, name='telegram-bot', daemon=True)
    _bot_thread.start()
    return _bot_thread
