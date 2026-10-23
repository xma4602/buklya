"""
Общие зависимости приложения, которые получают все боты.
"""

from __future__ import annotations

from dataclasses import dataclass

from buklya.config import Settings
from buklya.crypto import CredentialCipher
from buklya.db.database import Database
from buklya.db.repository import Repository
from buklya.mail.providers import ProviderRegistry
from buklya.services.mail_service import MailService


@dataclass
class AppContext:
    """Инфраструктура, общая для Telegram и ВКонтакте: настройки, БД, почта, провайдеры."""

    settings: Settings
    db: Database
    repo: Repository
    mail: MailService
    providers: ProviderRegistry

    @classmethod
    async def create(cls, settings: Settings) -> "AppContext":
        """Создаёт подключения и таблицы БД."""
        cipher = CredentialCipher(settings.encryption_key, settings.old_encryption_keys)
        db = Database(settings.database_url, echo=settings.database_echo)
        await db.create_tables()
        return cls(
            settings=settings,
            db=db,
            repo=Repository(db, cipher),
            mail=MailService(
                timeout=settings.mail_timeout,
                concurrency=settings.poll_concurrency,
                max_attachment_size=settings.max_attachment_size,
            ),
            providers=ProviderRegistry.from_file(settings.providers_file),
        )

    async def close(self) -> None:
        await self.db.close()
