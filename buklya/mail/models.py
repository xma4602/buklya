"""
Простые структуры данных почтового слоя (без зависимости от БД и Telegram).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MailCredentials:
    """Учётные данные и параметры серверов одного ящика (хранятся зашифрованными)."""

    email: str
    login: str
    password: str
    imap_host: str
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = 465
    #: "ssl" (порт 465), "starttls" (порт 587) или "none"
    smtp_security: str = "ssl"
    #: Имя отправителя в поле From (необязательно)
    display_name: str = ""
    #: Класть копию отправленного письма в «Отправленные» через IMAP.
    #: Gmail, Яндекс и Mail.ru делают это сами — для них False, чтобы не было дублей.
    save_to_sent: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailCredentials":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Attachment:
    """Вложение письма (входящего или исходящего)."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class ParsedEmail:
    """Разобранное входящее письмо."""

    uid: int
    subject: str = ""
    from_: str = ""
    from_addr: str = ""
    reply_to: str = ""
    to: str = ""
    cc: str = ""
    date: str = ""
    message_id: str = ""
    references: str = ""
    #: Текст письма (plain, либо HTML, переведённый в текст)
    text: str = ""
    #: Исходный HTML письма (если был)
    html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    #: Имена вложений, пропущенных из-за размера
    skipped_attachments: list[str] = field(default_factory=list)

    def reply_meta(self) -> dict[str, str]:
        """Метаданные, необходимые для ответа (сохраняются в БД зашифрованными)."""
        return {
            "from": self.from_,
            "from_addr": self.from_addr,
            "reply_to": self.reply_to,
            "to": self.to,
            "cc": self.cc,
            "subject": self.subject,
            "message_id": self.message_id,
            "references": self.references,
            "date": self.date,
        }


@dataclass
class MessageHeader:
    """Краткая информация о письме для списка «последние письма»."""

    uid: int
    subject: str
    from_: str
    date: str
    seen: bool = True
