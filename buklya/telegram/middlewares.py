"""
Middleware: ограничение доступа, определение пользователя Букли и журналирование действий.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from buklya.db.repository import Repository
from buklya.telegram import PLATFORM
from buklya.telegram.keyboards import BTN_ACCOUNTS, BTN_ADD, BTN_COMPOSE, BTN_HELP, BTN_LAST, BTN_LINK
from buklya.telegram.texts import t

MENU_BUTTONS = {BTN_ACCOUNTS, BTN_ADD, BTN_COMPOSE, BTN_HELP, BTN_LAST, BTN_LINK}

logger = logging.getLogger("buklya.actions")


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из TELEGRAM_ALLOWED_USERS (если список задан)."""

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
                await event.answer(t("common.access_denied"))
            elif isinstance(event, CallbackQuery):
                await event.answer(t.plain("common.access_denied_short"), show_alert=True)
            return None
        return await handler(event, data)


class IdentityMiddleware(BaseMiddleware):
    """
    Находит (или создаёт) пользователя Букли по аккаунту Telegram и передаёт
    его ID обработчикам аргументом `user_id`. Один пользователь Букли может быть
    привязан к нескольким мессенджерам — тогда ящики у них общие.
    """

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            data["user_id"] = await self.repo.resolve_user(PLATFORM, user.id)
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
        state = data.get("raw_state")
        user_id = data.get("user_id")

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
