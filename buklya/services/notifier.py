"""
Интерфейс доставки уведомлений пользователю.

Фоновая проверка почты (`MailPoller`) ничего не знает о конкретных мессенджерах:
она передаёт письмо «уведомителю» платформы вместе с ID пользователя на этой
платформе. Каждая платформа реализует этот интерфейс у себя
(`buklya.telegram.notifier`, `buklya.vk.notifier`).
"""

from __future__ import annotations

from typing import Protocol

from buklya.db.repository import AccountInfo
from buklya.mail.models import ParsedEmail


class RecipientUnavailable(Exception):
    """Пользователь недоступен (заблокировал бота, запретил сообщения от сообщества)."""


class Notifier(Protocol):
    """Отправка писем и служебных уведомлений в мессенджер."""

    #: Имя платформы (buklya.platforms.PLATFORMS)
    platform: str

    async def send_email(self, external_id: str, *, account_title: str, email: ParsedEmail, ref_id: int) -> None:
        """
        Показывает новое письмо пользователю.

        :param external_id: ID пользователя на платформе (identities.external_id)
        :raises RecipientUnavailable: если в этот мессенджер больше нельзя писать
        """

    async def notify_deactivated(self, external_id: str, acc: AccountInfo) -> None:
        """Сообщает, что проверка ящика приостановлена из-за ошибок."""
