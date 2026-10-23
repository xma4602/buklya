"""
Доставка новых писем во ВКонтакте для фоновой проверки почты.
"""

from __future__ import annotations

from buklya.db.repository import AccountInfo
from buklya.mail.models import ParsedEmail
from buklya.vk import PLATFORM
from buklya.vk.api import VkGateway
from buklya.vk.handlers.accounts import notify_deactivated
from buklya.vk.render import send_email


class VkNotifier:
    """
    Реализация `buklya.services.notifier.Notifier` для ВКонтакте.

    Если пользователь запретил сообщения от сообщества, gateway бросает
    `VkUserUnavailable` (наследник `RecipientUnavailable`) — поллер перестанет туда писать.
    """

    platform = PLATFORM

    def __init__(self, api: VkGateway, attach_html: bool = True) -> None:
        self.api = api
        self.attach_html = attach_html

    async def send_email(self, external_id: str, *, account_title: str, email: ParsedEmail, ref_id: int) -> None:
        await send_email(
            self.api,
            int(external_id),
            account_title=account_title,
            email=email,
            ref_id=ref_id,
            attach_html=self.attach_html,
        )

    async def notify_deactivated(self, external_id: str, acc: AccountInfo) -> None:
        await notify_deactivated(self.api, int(external_id), acc)
