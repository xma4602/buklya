"""
Общие части отображения письма (одинаковые для всех платформ).

Возвращают «безопасный HTML» (`buklya.i18n.Safe`): Telegram показывает его
напрямую, ВКонтакте переводит в своё форматирование (`buklya.vk.markup`).
"""

from __future__ import annotations

from buklya.i18n import Safe, Translator
from buklya.mail.models import ParsedEmail


def clip(text: str, limit: int = 300) -> str:
    """Обрезает слишком длинную строку (например, список из сотни адресатов)."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def email_header_html(tr: Translator, account_title: str, email: ParsedEmail, title: str) -> Safe:
    """Шапка письма: ящик, отправитель, тема, дата, вложения."""
    lines = [
        tr("email.header_title", title=title, account=clip(account_title, 100)),
        tr("email.from", value=clip(email.from_)),
    ]
    if email.to:
        lines.append(tr("email.to", value=clip(email.to)))
    if email.cc:
        lines.append(tr("email.cc", value=clip(email.cc)))
    lines.append(tr("email.subject", value=clip(email.subject)))
    if email.date:
        lines.append(tr("email.date", value=clip(email.date)))
    if email.attachments:
        names = clip(", ".join(a.filename for a in email.attachments), 500)
        lines.append(tr("email.attachments", count=len(email.attachments), names=names))
    if email.skipped_attachments:
        lines.append(tr("email.skipped", names=clip(", ".join(email.skipped_attachments), 500)))
    return Safe("\n".join(lines))


def has_multiple_recipients(email: ParsedEmail) -> bool:
    """Нужна ли кнопка «Ответить всем»."""
    return bool(email.cc) or "," in (email.to or "")
