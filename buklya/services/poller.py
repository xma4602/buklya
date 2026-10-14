"""
Фоновая проверка почтовых ящиков.

Раз в `POLL_INTERVAL` секунд проходит по всем ящикам с включёнными
уведомлениями, забирает новые письма (UID > last_uid) и отправляет их
владельцу в Telegram. После каждого отправленного письма last_uid
сохраняется — при перезапуске бот продолжит с того же места и не
пришлёт письма повторно.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from buklya.config import Settings
from buklya.db.repository import AccountInfo, Repository
from buklya.services.mail_service import MailService
from buklya.telegram.handlers.accounts import notify_deactivated
from buklya.telegram.render import send_email

logger = logging.getLogger(__name__)

#: После скольких ошибок подряд ящик приостанавливается
MAX_ERRORS = 10


class MailPoller:
    """Периодическая проверка новых писем."""

    def __init__(self, bot: Bot, repo: Repository, mail: MailService, settings: Settings) -> None:
        self.bot = bot
        self.repo = repo
        self.mail = mail
        self.settings = settings
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        """Основной цикл (запускается как фоновая задача)."""
        logger.info("Проверка почты запущена, интервал %s с", self.settings.poll_interval)
        while not self._stopped.is_set():
            try:
                await self.poll_all()
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                logger.exception("Ошибка цикла проверки почты")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.settings.poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("Проверка почты остановлена")

    def stop(self) -> None:
        self._stopped.set()

    async def poll_all(self) -> None:
        """Проверяет все активные ящики параллельно (параллелизм ограничен в MailService)."""
        accounts = await self.repo.accounts_for_polling()
        if not accounts:
            return
        logger.debug("Проверка %d ящиков", len(accounts))
        await asyncio.gather(*(self.poll_account(acc) for acc in accounts))

    async def poll_account(self, acc: AccountInfo) -> None:
        """Проверяет один ящик и пересылает новые письма."""
        try:
            result = await self.mail.poll(acc.credentials, acc.last_uid, acc.uid_validity)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ящик id=%s (user=%s): ошибка проверки: %s", acc.id, acc.user_id, exc)
            deactivated = await self.repo.save_poll_state(acc.id, error=str(exc), max_errors=MAX_ERRORS)
            if deactivated:
                logger.warning("Ящик id=%s приостановлен после %d ошибок", acc.id, MAX_ERRORS)
                acc.last_error = str(exc)
                await notify_deactivated(self.bot, acc)
            return

        if result.reset:
            logger.info("Ящик id=%s: сменился UIDVALIDITY, состояние сброшено", acc.id)

        for email in result.messages:
            ref_id = await self.repo.save_message(acc, email.uid, result.uid_validity, email.reply_meta())
            try:
                await send_email(
                    self.bot,
                    acc.user_id,
                    account_title=acc.title,
                    email=email,
                    ref_id=ref_id,
                    attach_html=self.settings.attach_html_original,
                )
                logger.info("Ящик id=%s: письмо uid=%s отправлено user=%s", acc.id, email.uid, acc.user_id)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — выключаем уведомления
                logger.warning("user=%s заблокировал бота, уведомления для ящика id=%s выключены", acc.user_id, acc.id)
                await self.repo.set_notify(acc.user_id, acc.id, False)
                return
            except Exception:  # noqa: BLE001
                logger.exception("Ящик id=%s: не удалось отправить письмо uid=%s в Telegram", acc.id, email.uid)
            # Запоминаем прогресс после каждого письма
            await self.repo.save_poll_state(acc.id, last_uid=email.uid, uid_validity=result.uid_validity)

        await self.repo.save_poll_state(acc.id, last_uid=result.last_uid, uid_validity=result.uid_validity)
