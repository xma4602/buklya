"""
Сборка исходящего письма (MIME) из того, что пользователь написал в чате (Telegram или ВКонтакте).

Письмо формируется как multipart/mixed:
    ├── multipart/alternative
    │     ├── text/plain  — текст без разметки
    │     └── text/html   — текст с форматированием (жирный, курсив, ссылки, код…)
    └── вложения (файлы, фото, голосовые и т.д.)
"""

from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

from buklya.i18n import t
from buklya.mail.models import Attachment, MailCredentials

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head>"
    "<body style=\"font-family: Arial, sans-serif; font-size: 14px;\">{body}</body></html>"
)


def format_address(name: str, addr: str) -> str:
    """
    «Имя <адрес>» в читаемом виде (без кодирования =?utf-8?...?=).
    Кодирование для заголовков письма выполнит сам модуль email при отправке.
    """
    name = (name or "").strip().replace('"', "'")
    if not name:
        return addr
    if any(ch in name for ch in ',;:<>@()[]\\.'):
        name = f'"{name}"'
    return f"{name} <{addr}>"


def parse_recipients(value: str) -> list[str]:
    """
    Разбирает строку получателей («a@x.ru, Иван <b@y.ru>; c@z.ru»).

    :raises ValueError: если какой-то адрес некорректен
    """
    value = value.replace(";", ",")
    result = []
    for name, addr in getaddresses([value]):
        addr = addr.strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            raise ValueError(t.plain("mail.bad_address", address=addr))
        result.append(format_address(name, addr))
    if not result:
        raise ValueError(t.plain("mail.no_address"))
    return result


def reply_subject(subject: str) -> str:
    """Добавляет «Re: » к теме, если его ещё нет."""
    subject = subject or ""
    if re.match(r"^\s*(re|ответ)\s*:", subject, re.IGNORECASE):
        return subject
    return f"Re: {subject}".strip()


def chat_html_to_email_html(tg_html: str) -> str:
    """
    HTML-разметку сообщений из чата переводим в HTML для письма: переносы строк -> <br>,
    спойлер -> обычный текст, цитаты — с отступом.

    Telegram отдаёт разметку b, i, u, s, a, code, pre, blockquote, tg-spoiler;
    из ВКонтакте приходят b, i, u, a (см. `buklya.vk.markup.vk_to_html`).
    """
    html = tg_html
    html = re.sub(r"<tg-spoiler>(.*?)</tg-spoiler>", r"\1", html, flags=re.S)
    html = re.sub(r'<span class="tg-spoiler">(.*?)</span>', r"\1", html, flags=re.S)
    html = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", html, flags=re.S)
    html = html.replace("<blockquote expandable>", "<blockquote>")
    html = html.replace(
        "<blockquote>", '<blockquote style="border-left:3px solid #ccc;margin:0 0 0 4px;padding-left:8px;">'
    )
    # Внутри <pre> переносы сохраняются сами; вне — заменяем на <br>
    parts = re.split(r"(<pre>.*?</pre>)", html, flags=re.S)
    parts = [p if p.startswith("<pre>") else p.replace("\n", "<br>\n") for p in parts]
    return "".join(parts)


#: Прежнее имя функции (сохранено для совместимости)
telegram_html_to_email_html = chat_html_to_email_html


def guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    """MIME-тип по имени файла."""
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or fallback


def build_message(
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
) -> EmailMessage:
    """
    Собирает письмо.

    :param text: текст без форматирования (text/plain)
    :param html_body: HTML-фрагмент (содержимое <body>)
    :param in_reply_to: Message-ID письма, на которое отвечаем
    :param references: цепочка References исходного письма
    """
    msg = EmailMessage()
    msg["From"] = format_address(creds.display_name, creds.email)
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(creds.email)[1].rsplit("@", 1)[-1] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["X-Mailer"] = "Buklya Mail Bot"

    # Заголовки, связывающие ответ с исходным письмом (для «цепочек» в почтовиках)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = f"{references} {in_reply_to}".strip() if in_reply_to not in references else references
        msg["References"] = refs

    msg.set_content(text or " ")
    if html_body:
        msg.add_alternative(HTML_TEMPLATE.format(body=html_body), subtype="html")

    for att in attachments or []:
        ctype = att.content_type or guess_content_type(att.filename)
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            att.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg
