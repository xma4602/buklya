"""
Отображение писем во ВКонтакте.

Письмо превращается в одно или несколько сообщений (лимит ВКонтакте — 4096
символов), затем отдельными сообщениями отправляются вложения: картинки —
как фото, остальное — как документы.
"""

from __future__ import annotations

import io
import logging
import zipfile

from buklya.i18n import Safe, escape
from buklya.mail.models import Attachment, ParsedEmail
from buklya.services.render import email_header_html, has_multiple_recipients
from buklya.vk.api import VkApiError, VkGateway, VkUserUnavailable
from buklya.vk.keyboards import email_actions
from buklya.vk.markup import VK_TEXT_LIMIT, html_to_vk, split_plain
from buklya.vk.texts import t

logger = logging.getLogger(__name__)

#: Максимум сообщений с текстом одного письма; остальное — файлом
MAX_BODY_MESSAGES = 5

#: Картинки, которые отправляются как фото (остальные файлы — документами)
PHOTO_TYPES = {"image/jpeg", "image/png", "image/gif"}
#: ВКонтакте не принимает фото больше 50 МБ
MAX_PHOTO_SIZE = 50 * 1024 * 1024


async def upload_attachment(api: VkGateway, peer_id: int, att: Attachment) -> str:
    """Загружает вложение во ВКонтакте; картинки — как фото, при ошибке — как документ."""
    if att.content_type in PHOTO_TYPES and att.size <= MAX_PHOTO_SIZE:
        try:
            return await api.upload_photo(peer_id, att.content, att.filename)
        except VkUserUnavailable:
            raise
        except VkApiError as exc:
            logger.info("Фото %s не загрузилось (%s), отправляю документом", att.filename, exc)
    return await api.upload_doc(peer_id, att.content, att.filename)


async def send_email(
    api: VkGateway,
    peer_id: int,
    *,
    account_title: str,
    email: ParsedEmail,
    ref_id: int,
    title: str | None = None,
    attach_html: bool = True,
) -> None:
    """Отправляет письмо в чат: шапка + текст + кнопки + вложения."""
    header = email_header_html(t, account_title, email, title or t.plain("email.new"))
    separator = Safe("\n" + t("email.separator") + "\n")
    body = email.text or t.plain("email.empty_body")

    # Первая часть должна уместиться вместе с шапкой (длина считается по тексту без разметки)
    header_len = len(html_to_vk(header + separator).text)
    part_limit = max(VK_TEXT_LIMIT - header_len - 10, 1000)
    chunks = [escape(chunk) for chunk in split_plain(body, part_limit)]
    chunks[0] = header + separator + chunks[0]

    truncated = len(chunks) > MAX_BODY_MESSAGES
    if truncated:
        chunks = chunks[:MAX_BODY_MESSAGES]
        chunks[-1] += "\n\n" + t("email.too_long")

    keyboard = email_actions(ref_id, has_multiple_recipients(email))
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        await api.send(peer_id, html_to_vk(chunk), keyboard=keyboard if is_last else None)

    # Вложения письма
    for att in email.attachments:
        try:
            attachment = await upload_attachment(api, peer_id, att)
            await api.send(peer_id, html_to_vk(t("email.attachment_caption", name=att.filename)), attachment=attachment)
        except VkUserUnavailable:
            raise
        except VkApiError as exc:
            logger.warning("Не удалось отправить вложение %s: %s", att.filename, exc)
            await api.send(peer_id, html_to_vk(t("email.attachment_failed", name=att.filename, error=str(exc))))

    # Полный текст, если он не поместился
    if truncated:
        await _send_file(api, peer_id, body.encode("utf-8"), "letter.txt", t("email.full_text_caption"))
    # Оригинальная HTML-версия (картинки, таблицы и вёрстка видны в браузере)
    # ВКонтакте не принимает .html-документы, поэтому оригинал отправляется в zip
    if attach_html and email.html.strip():
        await _send_file(api, peer_id, zip_single("letter.html", email.html.encode("utf-8")), "letter.zip",
                         t("email.html_caption"))


def zip_single(filename: str, data: bytes) -> bytes:
    """Архив из одного файла (в памяти)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, data)
    return buffer.getvalue()


async def _send_file(api: VkGateway, peer_id: int, data: bytes, filename: str, caption: str) -> None:
    """Служебный файл (полный текст / HTML-оригинал); ошибка загрузки не критична."""
    try:
        attachment = await api.upload_doc(peer_id, data, filename)
        await api.send(peer_id, html_to_vk(caption), attachment=attachment)
    except VkUserUnavailable:
        raise
    except VkApiError as exc:
        logger.info("Не удалось отправить %s: %s", filename, exc)
