"""
Разбор входящих писем (RFC 822 / MIME) в структуру `ParsedEmail`.
"""

from __future__ import annotations

import logging
import re
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr

from bs4 import BeautifulSoup

from buklya.i18n import t
from buklya.mail.models import Attachment, MessageHeader, ParsedEmail

logger = logging.getLogger(__name__)


# ============================================================================ заголовки
def decode_mime_header(value: str | None) -> str:
    """Декодирует заголовок вида `=?utf-8?b?...?=` в обычную строку."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:  # noqa: BLE001 — встречаются битые заголовки
        return str(value).strip()


def format_addresses(value: str | None) -> str:
    """Человекочитаемый список адресов: `Иван <ivan@x.ru>, petr@y.ru`."""
    if not value:
        return ""
    parts = []
    for name, addr in getaddresses([decode_mime_header(value)]):
        if name and addr:
            parts.append(f"{name} <{addr}>")
        elif addr:
            parts.append(addr)
        elif name:
            parts.append(name)
    return ", ".join(parts)


def _header(msg: Message, name: str) -> str:
    """Безопасно достаёт заголовок как строку."""
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 — policy.default может падать на кривых заголовках
        value = None
        for key, raw in msg.raw_items() if hasattr(msg, "raw_items") else []:
            if key.lower() == name.lower():
                value = raw
                break
    return decode_mime_header(value) if value is not None else ""


# ============================================================================ HTML -> текст
_BLOCK_TAGS = ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "section")


def html_to_text(html: str) -> str:
    """
    Превращает HTML-письмо в читаемый текст.

    Сохраняет переносы строк, пункты списков и адреса ссылок; скрипты и стили
    отбрасываются.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("• ")
    for link in soup.find_all("a"):
        href = (link.get("href") or "").strip()
        text = link.get_text(" ", strip=True)
        if href.startswith(("http://", "https://")) and href != text:
            link.replace_with(f"{text} ({href})" if text else href)
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    text = soup.get_text()
    return normalize_text(text)


def normalize_text(text: str) -> str:
    """Убирает лишние пробелы и пустые строки."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ============================================================================ письмо целиком
def _part_text(part: Message) -> str:
    """Текстовое содержимое MIME-части с учётом кодировки."""
    try:
        if isinstance(part, EmailMessage):
            return part.get_content()
    except Exception:  # noqa: BLE001 — неизвестная/ошибочная кодировка
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "cp1251", "koi8-r", "latin-1"):
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _attachment_name(part: Message, index: int) -> str:
    """Имя файла вложения (декодированное) или сгенерированное."""
    name = part.get_filename()
    if name:
        return decode_mime_header(name)
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "text/calendar": ".ics",
        "message/rfc822": ".eml",
    }.get(part.get_content_type(), ".bin")
    return f"attachment_{index}{ext}"


def parse_email(raw: bytes, uid: int = 0, max_attachment_size: int | None = None) -> ParsedEmail:
    """
    Разбирает «сырое» письмо.

    :param raw: байты письма (RFC 822)
    :param uid: UID письма в IMAP-папке
    :param max_attachment_size: вложения больше этого размера не загружаются в память
                                результата (их имена попадают в `skipped_attachments`)
    """
    msg = message_from_bytes(raw, policy=policy.default)

    from_raw = _header(msg, "From")
    result = ParsedEmail(
        uid=uid,
        subject=_header(msg, "Subject") or t.plain("common.no_subject"),
        from_=format_addresses(from_raw),
        from_addr=parseaddr(from_raw)[1],
        reply_to=format_addresses(_header(msg, "Reply-To")),
        to=format_addresses(_header(msg, "To")),
        cc=format_addresses(_header(msg, "Cc")),
        date=_header(msg, "Date"),
        message_id=_header(msg, "Message-ID"),
        references=_header(msg, "References"),
    )

    plain_parts: list[str] = []
    html_parts: list[str] = []
    index = 0

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        is_attachment = disposition == "attachment" or (
            part.get_filename() is not None and ctype not in ("text/plain", "text/html")
        )
        # Встроенные картинки (inline с Content-ID), календари и т.п. отдаём как вложения
        if not is_attachment and ctype not in ("text/plain", "text/html"):
            is_attachment = True

        if is_attachment:
            index += 1
            name = _attachment_name(part, index)
            payload = part.get_payload(decode=True)
            if payload is None and ctype == "message/rfc822":
                payload = part.as_bytes()
            payload = payload or b""
            if max_attachment_size and len(payload) > max_attachment_size:
                result.skipped_attachments.append(t.plain("mail.size_mb", name=name, size=len(payload) // 1024 // 1024))
                continue
            result.attachments.append(Attachment(filename=name, content=payload, content_type=ctype))
        elif ctype == "text/plain":
            plain_parts.append(_part_text(part))
        elif ctype == "text/html":
            html_parts.append(_part_text(part))

    result.html = "\n".join(html_parts)
    if plain_parts and any(p.strip() for p in plain_parts):
        result.text = normalize_text("\n\n".join(plain_parts))
    elif html_parts:
        result.text = html_to_text(result.html)
    return result


def parse_header_only(raw: bytes, uid: int, seen: bool = True) -> MessageHeader:
    """Разбирает только заголовки письма (для списка последних писем)."""
    msg = message_from_bytes(raw, policy=policy.default)
    from_raw = _header(msg, "From")
    name, addr = parseaddr(from_raw)
    return MessageHeader(
        uid=uid,
        subject=_header(msg, "Subject") or t.plain("common.no_subject"),
        from_=decode_mime_header(name) or addr,
        date=_header(msg, "Date"),
        seen=seen,
    )
