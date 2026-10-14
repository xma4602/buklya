"""
Асинхронный фасад над почтовым слоем.

imaplib/smtplib блокирующие, поэтому каждая операция выполняется в отдельном
потоке (`asyncio.to_thread`), а количество одновременных подключений
ограничено семафором.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from buklya.mail.composer import build_message
from buklya.mail.imap_client import ImapClient, ImapError
from buklya.mail.models import Attachment, MailCredentials, MessageHeader, ParsedEmail
from buklya.mail.parser import parse_email
from buklya.mail.smtp_client import SmtpError, check_smtp, send_message

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    """Результат проверки ящика на новые письма."""

    uid_validity: int
    last_uid: int
    messages: list[ParsedEmail]
    #: True — если UIDVALIDITY сменился и состояние ящика было сброшено
    reset: bool = False


class MailService:
    """Высокоуровневые операции с почтой."""

    #: Сколько новых писем максимум забирать за один проход (защита от лавины)
    MAX_NEW_PER_POLL = 20

    def __init__(self, timeout: int = 30, concurrency: int = 5, max_attachment_size: int | None = None) -> None:
        self.timeout = timeout
        self.max_attachment_size = max_attachment_size
        self._sem = asyncio.Semaphore(concurrency)

    async def _run(self, func, *args):  # noqa: ANN001
        """Выполняет блокирующую функцию в потоке под семафором."""
        async with self._sem:
            return await asyncio.to_thread(func, *args)

    # ================================================================ проверка
    async def check_credentials(self, creds: MailCredentials) -> tuple[int, int]:
        """
        Проверяет IMAP и SMTP. Возвращает (UIDVALIDITY, последний UID) —
        чтобы после подключения ящика не присылать все старые письма.
        """

        def _imap() -> tuple[int, int]:
            with ImapClient(creds, self.timeout) as client:
                validity, uidnext, _ = client.inbox_status()
                return validity, max(uidnext - 1, 0)

        result = await self._run(_imap)
        if creds.smtp_host:
            await self._run(check_smtp, creds, self.timeout)
        return result

    # ================================================================ новые письма
    async def poll(self, creds: MailCredentials, last_uid: int, uid_validity: int) -> PollResult:
        """Забирает письма, пришедшие после `last_uid`."""

        def _poll() -> PollResult:
            with ImapClient(creds, self.timeout) as client:
                validity, uidnext, _ = client.inbox_status()
                # UIDVALIDITY сменился (папку пересоздали) — старые UID недействительны.
                # Просто запоминаем текущее состояние, чтобы не прислать всю папку заново.
                if uid_validity and validity != uid_validity:
                    return PollResult(validity, max(uidnext - 1, 0), [], reset=True)
                if uidnext and uidnext - 1 <= last_uid:
                    return PollResult(validity, last_uid, [])

                uids = client.new_uids(last_uid)
                if len(uids) > self.MAX_NEW_PER_POLL:
                    logger.warning("Новых писем %d, обрабатываем последние %d", len(uids), self.MAX_NEW_PER_POLL)
                    uids = uids[-self.MAX_NEW_PER_POLL :]
                messages = []
                for uid in uids:
                    raw = client.fetch_raw(uid)
                    if raw is None:
                        continue
                    messages.append(parse_email(raw, uid, self.max_attachment_size))
                new_last = max(uids) if uids else last_uid
                return PollResult(validity, max(new_last, last_uid), messages)

        return await self._run(_poll)

    # ================================================================ просмотр
    async def recent(self, creds: MailCredentials, offset: int, limit: int) -> tuple[list[MessageHeader], int, int]:
        """Заголовки последних писем: (список, всего писем, UIDVALIDITY)."""

        def _recent():
            with ImapClient(creds, self.timeout) as client:
                validity, _, _ = client.inbox_status()
                headers, total = client.recent_headers(offset, limit)
                return headers, total, validity

        return await self._run(_recent)

    async def fetch(self, creds: MailCredentials, uid: int) -> ParsedEmail | None:
        """Одно письмо целиком."""

        def _fetch():
            with ImapClient(creds, self.timeout) as client:
                raw = client.fetch_raw(uid)
                return parse_email(raw, uid, self.max_attachment_size) if raw else None

        return await self._run(_fetch)

    async def mark_seen(self, creds: MailCredentials, uid: int) -> None:
        """Помечает письмо прочитанным."""

        def _mark():
            with ImapClient(creds, self.timeout) as client:
                client.mark_seen(uid)

        await self._run(_mark)

    # ================================================================ отправка
    async def send(
        self,
        creds: MailCredentials,
        *,
        to: list[str],
        subject: str,
        text: str,
        html_body: str,
        cc: list[str] | None = None,
        attachments: list[Attachment] | None = None,
        in_reply_to: str = "",
        references: str = "",
    ) -> None:
        """Собирает и отправляет письмо; при необходимости кладёт копию в «Отправленные»."""
        message = build_message(
            creds,
            to=to,
            cc=cc,
            subject=subject,
            text=text,
            html_body=html_body,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
        )
        await self._run(send_message, creds, message, self.timeout)

        if creds.save_to_sent:

            def _append():
                with ImapClient(creds, self.timeout) as client:
                    return client.append_to_sent(bytes(message))

            try:
                await self._run(_append)
            except ImapError as exc:  # копия в «Отправленных» — не критично
                logger.warning("Не удалось сохранить копию в «Отправленные»: %s", exc)


__all__ = ["MailService", "PollResult", "ImapError", "SmtpError"]
