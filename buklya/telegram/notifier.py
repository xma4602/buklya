"""
Доставка новых писем в Telegram для фоновой проверки почты.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from buklya.db.repository import AccountInfo
from buklya.mail.models import ParsedEmail
from buklya.services.notifier import RecipientUnavailable
from buklya.telegram import PLATFORM
from buklya.telegram.handlers.accounts import notify_deactivated
from buklya.telegram.render import send_email


class TelegramNotifier:
    """Реализация `buklya.services.notifier.Notifier` для Telegram."""

    platform = PLATFORM

    def __init__(self, bot: Bot, attach_html: bool = True) -> None:
        self.bot = bot
        self.attach_html = attach_html

    async def send_email(self, external_id: str, *, account_title: str, email: ParsedEmail, ref_id: int) -> None:
        try:
            await send_email(
                self.bot,
                int(external_id),
                account_title=account_title,
                email=email,
                ref_id=ref_id,
                attach_html=self.attach_html,
            )
        except TelegramForbiddenError as exc:  # пользователь заблокировал бота
            raise RecipientUnavailable(str(exc)) from exc

    async def notify_deactivated(self, external_id: str, acc: AccountInfo) -> None:
        await notify_deactivated(self.bot, int(external_id), acc)
