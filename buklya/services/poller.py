"""
Фоновая проверка почтовых ящиков.

Раз в `POLL_INTERVAL` секунд проходит по ящикам с включёнными уведомлениями,
забирает новые письма и отправляет их во все мессенджеры владельца,
которые обслуживает этот процесс.

Боты разных платформ могут работать в разных процессах (контейнерах) с общей
базой. Поэтому прогресс хранится отдельно для каждой платформы
(`delivery_cursors`): процесс Telegram и процесс ВКонтакте проверяют ящик
независимо, и каждое письмо приходит в каждый мессенджер ровно один раз.
Если оба бота в одном процессе, ящик проверяется один раз за проход.

Платформа, у которой ещё нет курсора (например, пользователь только что привязал
новый мессенджер), начинает с самого нового уже обработанного письма — старые
письма не присылаются.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from buklya.config import Settings
from buklya.db.repository import AccountInfo, Repository
from buklya.services.mail_service import MailService
from buklya.services.notifier import Notifier, RecipientUnavailable

logger = logging.getLogger(__name__)

#: После скольких ошибок подряд ящик приостанавливается
MAX_ERRORS = 10


class MailPoller:
    """Периодическая проверка новых писем."""

    def __init__(
        self,
        repo: Repository,
        mail: MailService,
        settings: Settings,
        notifiers: Iterable[Notifier],
    ) -> None:
        self.repo = repo
        self.mail = mail
        self.settings = settings
        self.notifiers: dict[str, Notifier] = {n.platform: n for n in notifiers}
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        """Основной цикл (запускается как фоновая задача)."""
        logger.info(
            "Проверка почты запущена (%s), интервал %s с",
            ", ".join(self.notifiers),
            self.settings.poll_interval,
        )
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
        """Проверяет все нужные ящики параллельно (параллелизм ограничен в MailService)."""
        accounts = await self.repo.accounts_for_polling(self.notifiers.keys())
        if not accounts:
            return
        logger.debug("Проверка %d ящиков", len(accounts))
        await asyncio.gather(*(self.poll_account(acc) for acc in accounts))

    async def poll_account(self, acc: AccountInfo) -> None:
        """Проверяет один ящик и рассылает новые письма по мессенджерам владельца."""
        # Куда доставлять: мессенджеры владельца на платформах этого процесса
        recipients: dict[str, list[str]] = {}
        for identity in await self.repo.list_identities(acc.user_id):
            if identity.platform in self.notifiers and not identity.blocked:
                recipients.setdefault(identity.platform, []).append(identity.external_id)
        if not recipients:
            return

        # С какого письма продолжать для каждой платформы
        saved = await self.repo.get_cursors(acc.id)
        cursors: dict[str, int] = {}
        for platform in recipients:
            cursor = saved.get(platform)
            valid = cursor is not None and (not acc.uid_validity or cursor.uid_validity == acc.uid_validity)
            cursors[platform] = cursor.last_uid if valid else acc.last_uid
        start = min(cursors.values())

        try:
            result = await self.mail.poll(acc.credentials, start, acc.uid_validity)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ящик id=%s (user=%s): ошибка проверки: %s", acc.id, acc.user_id, exc)
            deactivated = await self.repo.save_poll_state(acc.id, error=str(exc), max_errors=MAX_ERRORS)
            if deactivated:
                logger.warning("Ящик id=%s приостановлен после %d ошибок", acc.id, MAX_ERRORS)
                acc.last_error = str(exc)
                await self._notify_deactivated(acc, recipients)
            return

        if result.reset:
            # UIDVALIDITY сменился — начинаем с текущего состояния папки, старое не присылаем
            logger.info("Ящик id=%s: сменился UIDVALIDITY, состояние сброшено", acc.id)
            for platform in recipients:
                await self.repo.save_cursor(acc.id, platform, result.last_uid, result.uid_validity)
            await self.repo.save_poll_state(acc.id, last_uid=result.last_uid, uid_validity=result.uid_validity)
            return

        for email in result.messages:
            ref_id = await self.repo.save_message(acc, email.uid, result.uid_validity, email.reply_meta())
            for platform, external_ids in recipients.items():
                if email.uid <= cursors[platform]:
                    continue  # в эту платформу уже доставлено
                await self._deliver(acc, platform, external_ids, email, ref_id)
                cursors[platform] = email.uid
                # Прогресс — после каждого письма: при перезапуске не будет повторов
                await self.repo.save_cursor(acc.id, platform, email.uid, result.uid_validity)

        for platform in recipients:
            cursors[platform] = max(cursors[platform], result.last_uid)
            await self.repo.save_cursor(acc.id, platform, cursors[platform], result.uid_validity)
        await self.repo.save_poll_state(acc.id, last_uid=result.last_uid, uid_validity=result.uid_validity)

    async def _deliver(self, acc: AccountInfo, platform: str, external_ids: list[str], email, ref_id: int) -> None:
        """Отправляет письмо во все мессенджеры владельца на платформе."""
        notifier = self.notifiers[platform]
        for external_id in list(external_ids):
            try:
                await notifier.send_email(external_id, account_title=acc.title, email=email, ref_id=ref_id)
                logger.info("Ящик id=%s: письмо uid=%s отправлено (%s, user=%s)", acc.id, email.uid, platform, acc.user_id)
            except RecipientUnavailable:
                # Пользователь заблокировал бота в этом мессенджере — больше туда не пишем
                logger.warning("user=%s недоступен в %s — доставка туда приостановлена", acc.user_id, platform)
                await self.repo.set_identity_blocked(platform, external_id, True)
                external_ids.remove(external_id)
            except Exception:  # noqa: BLE001
                logger.exception("Ящик id=%s: не удалось отправить письмо uid=%s (%s)", acc.id, email.uid, platform)

    async def _notify_deactivated(self, acc: AccountInfo, recipients: dict[str, list[str]]) -> None:
        for platform, external_ids in recipients.items():
            for external_id in external_ids:
                try:
                    await self.notifiers[platform].notify_deactivated(external_id, acc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Не удалось уведомить user=%s (%s): %s", acc.user_id, platform, exc)
