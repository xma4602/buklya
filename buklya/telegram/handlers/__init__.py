"""
Регистрация обработчиков.

Порядок роутеров важен: команды и кнопки главного меню должны срабатывать
раньше, чем обработчики «свободного текста» в диалогах (например, набор письма).
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from buklya.telegram.handlers import accounts, common, compose, inbox, link
from buklya.telegram.texts import t

fallback = Router(name="fallback")


@fallback.message(F.chat.type == "private")
async def unknown_message(message: Message) -> None:
    """Сообщение вне какого-либо диалога."""
    await message.answer(t("common.unknown_message"))


@fallback.callback_query()
async def unknown_callback(call: CallbackQuery) -> None:
    """Устаревшие или неизвестные кнопки."""
    await call.answer(t.plain("common.stale_button"), show_alert=False)


def setup_routers() -> Router:
    """Собирает все роутеры в один корневой."""
    root = Router(name="root")
    children = (common.router, link.router, accounts.router, inbox.router, compose.router, fallback)
    for child in children:
        # Роутеры — глобальные объекты модулей, а aiogram разрешает роутеру одного родителя.
        # Сбрасываем прежнего, чтобы диспетчер можно было собрать повторно (тесты).
        child._parent_router = None
    root.include_routers(*children)
    return root
