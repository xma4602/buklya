"""
Точка входа: `python -m buklya`.

Собирает приложение: настройки -> логирование -> шифрование -> БД ->
почтовый сервис -> Telegram-бот + фоновая проверка почты.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Запуск файлом (python buklya/__main__.py, «script» в PyCharm):
    # добавляем корень проекта в sys.path, чтобы работал `import buklya`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from buklya import __version__
from buklya.config import get_settings
from buklya.crypto import CredentialCipher
from buklya.db.database import Database
from buklya.db.repository import Repository
from buklya.logging_setup import setup_logging
from buklya.mail.providers import ProviderRegistry
from buklya.services.mail_service import MailService
from buklya.services.poller import MailPoller
from buklya.telegram.handlers import setup_routers
from buklya.telegram.middlewares import AccessMiddleware, ActionLogMiddleware

BOT_COMMANDS = [
    BotCommand(command="last", description="Последние письма"),
    BotCommand(command="compose", description="Написать письмо"),
    BotCommand(command="accounts", description="Мои ящики"),
    BotCommand(command="add", description="Подключить ящик"),
    BotCommand(command="cancel", description="Отменить действие"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    settings = get_settings()
    logger = setup_logging(settings.log_dir, settings.log_level, settings.log_retention_days)
    logger.info("Запуск Букли v%s", __version__)

    # --- инфраструктура
    cipher = CredentialCipher(settings.encryption_key, settings.old_encryption_keys)
    db = Database(settings.database_url, echo=settings.database_echo)
    await db.create_tables()
    repo = Repository(db, cipher)
    providers = ProviderRegistry.from_file(settings.providers_file)
    mail = MailService(
        timeout=settings.mail_timeout,
        concurrency=settings.poll_concurrency,
        max_attachment_size=settings.max_attachment_size,
    )

    # --- Telegram
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    # Зависимости, которые aiogram подставляет в обработчики по имени аргумента
    dp["repo"] = repo
    dp["mail"] = mail
    dp["settings"] = settings
    dp["providers"] = providers

    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(AccessMiddleware(settings.allowed_users))
        observer.middleware(ActionLogMiddleware())
    dp.include_router(setup_routers())

    poller: MailPoller | None = None
    poller_task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        logger.info("Бот @%s авторизован", me.username)
        await bot.set_my_commands(BOT_COMMANDS)

        # --- фоновая проверка почты
        poller = MailPoller(bot, repo, mail, settings)
        poller_task = asyncio.create_task(poller.run(), name="mail-poller")

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Остановка…")
        if poller is not None:
            poller.stop()
        if poller_task is not None:
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task
        await bot.session.close()
        await db.close()
        logger.info("Букля остановлена")


def run() -> None:
    """Синхронная обёртка для запуска."""
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        logging.getLogger("buklya").exception("Критическая ошибка, приложение остановлено")
        sys.exit(1)


if __name__ == "__main__":
    run()
