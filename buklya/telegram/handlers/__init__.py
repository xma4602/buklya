"""
Регистрация обработчиков.

Порядок роутеров важен: команды и кнопки главного меню должны срабатывать
раньше, чем обработчики «свободного текста» в диалогах (например, набор письма).
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from buklya.telegram.handlers import accounts, common, compose, inbox

fallback = Router(name="fallback")


@fallback.message(F.chat.type == "private")
async def unknown_message(message: Message) -> None:
    """Сообщение вне какого-либо диалога."""
    await message.answer("Не понимаю 🦉 Воспользуйтесь кнопками меню или /help.")


@fallback.callback_query()
async def unknown_callback(call: CallbackQuery) -> None:
    """Устаревшие или неизвестные кнопки."""
    await call.answer("Эта кнопка больше не активна", show_alert=False)


def setup_routers() -> Router:
    """Собирает все роутеры в один корневой."""
    root = Router(name="root")
    root.include_routers(common.router, accounts.router, inbox.router, compose.router, fallback)
    return root
