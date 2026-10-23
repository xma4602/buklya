"""
Сборка и запуск Telegram-бота.

Вызывается из точки входа (`buklya.__main__`) — отдельно или вместе с ботом ВКонтакте.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from buklya.app_context import AppContext
from buklya.services.linking import LinkService
from buklya.telegram import PLATFORM
from buklya.telegram.handlers import setup_routers
from buklya.telegram.middlewares import AccessMiddleware, ActionLogMiddleware, IdentityMiddleware
from buklya.telegram.notifier import TelegramNotifier
from buklya.telegram.settings import TelegramSettings, get_telegram_settings
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)


def create_app(ctx: AppContext) -> "TelegramApp":
    """Для реестра платформ (buklya.platforms)."""
    return TelegramApp(ctx)


def build_dispatcher(ctx: AppContext, allowed_users: list[int]) -> Dispatcher:
    """Dispatcher с обработчиками и middleware (используется и в тестах)."""
    dp = Dispatcher(storage=MemoryStorage())
    # Зависимости, которые aiogram подставляет в обработчики по имени аргумента
    dp["repo"] = ctx.repo
    dp["mail"] = ctx.mail
    dp["settings"] = ctx.settings
    dp["providers"] = ctx.providers
    dp["linking"] = LinkService(ctx.repo, ctx.settings.link_code_ttl_minutes)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(AccessMiddleware(allowed_users))
        observer.outer_middleware(IdentityMiddleware(ctx.repo))
        observer.middleware(ActionLogMiddleware())
    dp.include_router(setup_routers())
    return dp

#: Команды в меню Telegram (описания — из файла локализации)
COMMANDS = ("last", "compose", "accounts", "add", "link", "cancel", "help")


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=t.plain(f"commands.{name}")) for name in COMMANDS]


class TelegramApp:
    """Telegram-бот: aiogram Bot + Dispatcher с обработчиками Букли."""

    platform = PLATFORM

    def __init__(self, ctx: AppContext, tg_settings: TelegramSettings | None = None) -> None:
        settings = ctx.settings
        tg = tg_settings or get_telegram_settings()
        self.bot = Bot(tg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = build_dispatcher(ctx, tg.allowed_users)
        self.notifier = TelegramNotifier(self.bot, attach_html=settings.attach_html_original)

    async def start(self) -> None:
        """Проверяет токен и регистрирует команды (до запуска поллинга)."""
        me = await self.bot.get_me()
        logger.info("Telegram: бот @%s авторизован", me.username)
        await self.bot.set_my_commands(bot_commands())

    async def run(self) -> None:
        """Получение обновлений (работает до отмены задачи)."""
        await self.dp.start_polling(
            self.bot,
            allowed_updates=self.dp.resolve_used_update_types(),
            handle_signals=False,  # сигналы обрабатывает точка входа (общая для всех ботов)
            close_bot_session=False,
        )

    async def close(self) -> None:
        await self.bot.session.close()
