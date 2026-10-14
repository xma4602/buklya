"""
Middleware: ограничение доступа и журналирование действий пользователей.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from buklya.telegram.keyboards import BTN_ACCOUNTS, BTN_ADD, BTN_COMPOSE, BTN_HELP, BTN_LAST

MENU_BUTTONS = {BTN_ACCOUNTS, BTN_ADD, BTN_COMPOSE, BTN_HELP, BTN_LAST}

logger = logging.getLogger("buklya.actions")


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из ALLOWED_USERS (если список задан)."""

    def __init__(self, allowed: list[int]) -> None:
        self.allowed = set(allowed)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if self.allowed and (user is None or user.id not in self.allowed):
            logger.warning("Доступ запрещён: user_id=%s", getattr(user, "id", None))
            if isinstance(event, Message):
                await event.answer("⛔ У вас нет доступа к этому боту.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            return None
        return await handler(event, data)


class ActionLogMiddleware(BaseMiddleware):
    """
    Пишет в лог каждое действие пользователя.

    Текст сообщений НЕ логируется (там могут быть пароли и личная переписка) —
    только тип события, команда/кнопка и текущее состояние диалога.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        state = data.get("raw_state")
        user_id = getattr(user, "id", None)

        if isinstance(event, Message):
            if event.text and event.text.startswith("/"):
                what = f"команда {event.text.split()[0]}"
            elif event.text in MENU_BUTTONS:
                what = f"кнопка меню «{event.text}»"
            else:
                what = f"сообщение ({event.content_type})"
            logger.info("user=%s state=%s: %s", user_id, state, what)
        elif isinstance(event, CallbackQuery):
            logger.info("user=%s state=%s: кнопка %s", user_id, state, event.data)

        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Ошибка при обработке события от user=%s", user_id)
            raise
