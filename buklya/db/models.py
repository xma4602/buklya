"""
ORM-модели (SQLAlchemy 2.0, декларативный стиль).

Модели написаны без привязки к конкретной СУБД: используются только
переносимые типы, поэтому SQLite можно заменить на PostgreSQL/MySQL
сменой одной строки `DATABASE_URL`.

Чувствительные данные хранятся ТОЛЬКО в зашифрованном виде (поля `*_enc`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время в UTC (для значений по умолчанию)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""


class User(Base):
    """Пользователь Telegram."""

    __tablename__ = "users"

    #: Telegram user id (он же chat id личного чата с ботом)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    accounts: Mapped[list["MailAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class MailAccount(Base):
    """Подключённый почтовый ящик пользователя."""

    __tablename__ = "mail_accounts"
    __table_args__ = (UniqueConstraint("user_id", "email_index", name="uq_account_user_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    #: Зашифрованный JSON с адресом, логином, паролем и параметрами серверов
    credentials_enc: Mapped[str] = mapped_column(Text)
    #: Зашифрованное отображаемое имя ящика (обычно сам адрес)
    title_enc: Mapped[str] = mapped_column(Text)
    #: Слепой индекс адреса (HMAC) — для проверки на дубликаты без расшифровки
    email_index: Mapped[str] = mapped_column(String(64))

    #: Ключ пресета провайдера (yandex, gmail, ...) — не секрет
    provider: Mapped[str] = mapped_column(String(32), default="custom")

    #: Проверять ли ящик на новые письма
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Ящик временно отключён (например, после неверного пароля)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Последний обработанный UID во «Входящих» и UIDVALIDITY папки
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    uid_validity: Mapped[int] = mapped_column(BigInteger, default=0)

    #: Счётчик последовательных ошибок и текст последней (без секретов)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="accounts")


class MailMessageRef(Base):
    """
    Ссылка на письмо, показанное в чате.

    Нужна, чтобы по нажатию кнопки «Ответить» / «Открыть» знать, о каком письме
    речь (callback_data в Telegram ограничена 64 байтами). Метаданные письма
    (от кого, тема, Message-ID) хранятся зашифрованными.
    """

    __tablename__ = "mail_messages"
    __table_args__ = (UniqueConstraint("account_id", "uid_validity", "uid", name="uq_message_uid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True)
    uid: Mapped[int] = mapped_column(BigInteger)
    uid_validity: Mapped[int] = mapped_column(BigInteger, default=0)
    #: Зашифрованный JSON: from, reply_to, to, cc, subject, message_id, references, date
    meta_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
