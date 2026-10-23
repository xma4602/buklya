"""
Отображение писем в Telegram.

Письмо превращается в одно или несколько сообщений (лимит Telegram — 4096
символов на сообщение), затем отдельными файлами отправляются вложения.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from buklya.mail.models import ParsedEmail
from buklya.services.render import email_header_html, has_multiple_recipients
from buklya.telegram.keyboards import email_actions
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)

TG_LIMIT = 4096
#: Максимум сообщений с текстом одного письма; остальное — файлом
MAX_BODY_MESSAGES = 5


def esc(text: str) -> str:
    """Экранирование для parse_mode=HTML."""
    return html.escape(text or "", quote=False)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """
    Делит текст на части так, чтобы ПОСЛЕ html-экранирования каждая часть
    укладывалась в `limit`. Режет по границам строк, длинные строки — по символам.
    Возвращает уже экранированные части.
    """
    chunks: list[str] = []
    current = ""
    for line in (text or "").split("\n"):
        piece = esc(line) + "\n"
        # Строка сама по себе длиннее лимита — режем её
        while len(piece) > limit:
            cut = limit - len(current) if current else limit
            if cut <= 0:
                chunks.append(current)
                current = ""
                continue
            # Не разрываем html-сущность (&amp; и т.п.)
            amp = piece.rfind("&", max(cut - 6, 0), cut)
            if amp != -1 and ";" not in piece[amp:cut]:
                cut = amp
            chunks.append(current + piece[:cut])
            current = ""
            piece = piece[cut:]
        if len(current) + len(piece) > limit:
            chunks.append(current)
            current = ""
        current += piece
    if current.strip():
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()] or [""]


def email_header(account_title: str, email: ParsedEmail, title: str) -> str:
    """Шапка письма: ящик, отправитель, тема и т.д."""
    return email_header_html(t, account_title, email, title)


async def send_email(
    bot: Bot,
    chat_id: int,
    *,
    account_title: str,
    email: ParsedEmail,
    ref_id: int,
    title: str | None = None,
    attach_html: bool = True,
) -> None:
    """Отправляет письмо в чат: шапка + текст + кнопки + вложения."""
    header = email_header(account_title, email, title or t.plain("email.new"))
    separator = "\n" + t("email.separator") + "\n"
    body = email.text or t.plain("email.empty_body")

    # Режем текст так, чтобы первая часть уместилась вместе с шапкой
    # (остальные части того же размера — так проще и надёжнее)
    part_limit = max(TG_LIMIT - len(header) - len(separator) - 10, 1000)
    chunks = split_text(body, part_limit)
    chunks[0] = header + separator + chunks[0]

    truncated = len(chunks) > MAX_BODY_MESSAGES
    if truncated:
        chunks = chunks[:MAX_BODY_MESSAGES]
        chunks[-1] += "\n\n" + t("email.too_long")

    multiple = has_multiple_recipients(email)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await bot.send_message(
            chat_id,
            chunk,
            reply_markup=email_actions(ref_id, multiple) if is_last else None,
            disable_web_page_preview=True,
        )

    # Вложения письма
    for att in email.attachments:
        try:
            await bot.send_document(
                chat_id,
                BufferedInputFile(att.content, filename=att.filename),
                caption=t("email.attachment_caption", name=att.filename),
            )
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить вложение %s: %s", att.filename, exc)
            await bot.send_message(chat_id, t("email.attachment_failed", name=att.filename, error=str(exc)))

    # Полный текст, если он не поместился
    if truncated:
        await bot.send_document(
            chat_id, BufferedInputFile(body.encode("utf-8"), filename="letter.txt"), caption=t("email.full_text_caption")
        )
    # Оригинальная HTML-версия (картинки, таблицы и вёрстка видны в браузере)
    if attach_html and email.html.strip():
        await bot.send_document(
            chat_id,
            BufferedInputFile(email.html.encode("utf-8"), filename="letter.html"),
            caption=t("email.html_caption"),
        )
