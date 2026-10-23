"""
Общая логика черновика письма (не зависит от платформы).

Черновик — словарь в состоянии диалога пользователя:

    account_id, account_title  — ящик-отправитель
    to, cc                     — получатели («Имя <адрес>»)
    subject                    — тема
    in_reply_to, references    — связь с исходным письмом (для ответа)
    html_parts                 — сообщения пользователя в HTML (с форматированием)
    text_parts                 — те же сообщения без разметки
    files                      — вложения: {"name", "mime", "size", ...}
                                 (Telegram хранит file_id, ВКонтакте — сам файл)
    panel_id                   — сообщение с панелью черновика (чтобы перерисовывать)

Здесь собраны функции, которые одинаково нужны Telegram и ВКонтакте:
расчёт получателей ответа и тексты панели/предпросмотра.
"""

from __future__ import annotations

from email.utils import getaddresses, parseaddr
from typing import Any

from buklya.db.repository import AccountInfo, MessageInfo
from buklya.i18n import Safe, Translator, escape
from buklya.mail.composer import format_address, reply_subject


def empty_draft(account: AccountInfo, **extra: Any) -> dict[str, Any]:
    """Начальные данные черновика."""
    draft: dict[str, Any] = {
        "account_id": account.id,
        "account_title": account.title,
        "to": [],
        "cc": [],
        "subject": "",
        "in_reply_to": "",
        "references": "",
        "html_parts": [],
        "text_parts": [],
        "files": [],
        "panel_id": None,
    }
    draft.update(extra)
    return draft


def reply_draft(ref: MessageInfo, account: AccountInfo, reply_all: bool) -> dict[str, Any]:
    """
    Черновик ответа на письмо.

    Кому: Reply-To, если есть, иначе отправитель. «Ответить всем» добавляет в копию
    остальных адресатов исходного письма (кроме себя и уже указанных).
    """
    meta = ref.meta
    own = account.credentials.email.lower()
    to = [
        format_address(name, addr)
        for name, addr in getaddresses([meta.get("reply_to") or meta.get("from") or ""])
        if addr
    ]
    cc: list[str] = []
    if reply_all:
        seen = {parseaddr(item)[1].lower() for item in to} | {own}
        for name, addr in getaddresses([meta.get("to", ""), meta.get("cc", "")]):
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                cc.append(format_address(name, addr))
    return empty_draft(
        account,
        to=to,
        cc=cc,
        subject=reply_subject(meta.get("subject", "")),
        in_reply_to=meta.get("message_id", ""),
        references=meta.get("references", ""),
    )


def is_empty(data: dict[str, Any]) -> bool:
    """В черновике нет ни текста, ни вложений."""
    return not data.get("html_parts") and not data.get("files")


def panel_text(tr: Translator, data: dict[str, Any]) -> Safe:
    """Текст панели черновика (HTML)."""
    text_len = sum(len(part) for part in data.get("text_parts", []))
    parts = len(data.get("html_parts", []))
    files = data.get("files", [])
    lines = [
        tr("compose.draft_title"),
        tr("compose.draft_from", value=data.get("account_title", "")),
        tr("compose.draft_to", value=", ".join(data.get("to", [])) or tr.plain("compose.nobody")),
    ]
    if data.get("cc"):
        lines.append(tr("compose.draft_cc", value=", ".join(data["cc"])))
    lines.append(tr("compose.draft_subject", value=data.get("subject") or tr.plain("common.no_subject")))
    lines.append(tr("compose.draft_text", parts=parts, chars=text_len) if parts else tr("compose.draft_text_empty"))
    if files:
        lines.append(tr("compose.draft_files", count=len(files), names=", ".join(f["name"] for f in files)))
    lines.append("\n" + tr("compose.draft_hint"))
    return Safe("\n".join(lines))


def preview_parts(tr: Translator, data: dict[str, Any]) -> tuple[Safe, Safe, Safe]:
    """
    Предпросмотр письма: (шапка, тело, хвост со списком вложений) — всё в HTML.
    Платформа сама решает, как склеить и что делать, если текст слишком длинный.
    """
    separator = tr("email.separator")
    head = Safe(
        tr("compose.preview_head", to=", ".join(data.get("to", [])),
           subject=data.get("subject") or tr.plain("common.no_subject"))
        + "\n" + separator + "\n"
    )
    body = Safe("\n\n".join(data.get("html_parts", [])) or tr("compose.preview_empty"))
    files = data.get("files", [])
    tail = Safe("")
    if files:
        names = ", ".join(escape(f["name"]) for f in files)
        tail = Safe("\n" + separator + "\n📎 " + names)
    return head, body, tail


def sent_text(tr: Translator, account_title: str, data: dict[str, Any]) -> Safe:
    """Сообщение об успешной отправке."""
    return tr(
        "compose.sent",
        title=account_title,
        to=", ".join(data["to"]),
        subject=data.get("subject") or tr.plain("common.no_subject"),
    )
