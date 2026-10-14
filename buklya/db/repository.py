"""
Репозиторий — единственное место, где код работает с БД напрямую.

Остальные части приложения получают уже расшифрованные объекты
(`AccountInfo`, `MessageInfo`) и не знают ни о сессиях, ни о шифровании.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from buklya.crypto import CredentialCipher, CryptoError
from buklya.db.database import Database
from buklya.db.models import MailAccount, MailMessageRef, User
from buklya.mail.models import MailCredentials

logger = logging.getLogger(__name__)


class DuplicateAccountError(Exception):
    """Пользователь пытается добавить ящик, который уже подключён."""


@dataclass
class AccountInfo:
    """Расшифрованное представление почтового ящика."""

    id: int
    user_id: int
    title: str
    provider: str
    notify: bool
    is_active: bool
    last_uid: int
    uid_validity: int
    error_count: int
    last_error: str | None
    credentials: MailCredentials


@dataclass
class MessageInfo:
    """Расшифрованная ссылка на письмо."""

    id: int
    account_id: int
    uid: int
    uid_validity: int
    meta: dict[str, Any]


class Repository:
    """Операции с данными пользователей, ящиков и писем."""

    def __init__(self, db: Database, cipher: CredentialCipher) -> None:
        self.db = db
        self.cipher = cipher

    # ================================================================ helpers
    def _to_info(self, acc: MailAccount) -> AccountInfo:
        """Расшифровывает запись ящика."""
        creds = MailCredentials.from_dict(self.cipher.decrypt_json(acc.user_id, acc.credentials_enc))
        return AccountInfo(
            id=acc.id,
            user_id=acc.user_id,
            title=self.cipher.decrypt(acc.user_id, acc.title_enc),
            provider=acc.provider,
            notify=acc.notify,
            is_active=acc.is_active,
            last_uid=acc.last_uid,
            uid_validity=acc.uid_validity,
            error_count=acc.error_count,
            last_error=acc.last_error,
            credentials=creds,
        )

    def _safe_infos(self, rows: list[MailAccount]) -> list[AccountInfo]:
        """Расшифровывает список ящиков, пропуская записи, которые не расшифровываются."""
        result = []
        for acc in rows:
            try:
                result.append(self._to_info(acc))
            except CryptoError:
                logger.error("Ящик id=%s не расшифровывается — проверьте ENCRYPTION_KEY", acc.id)
        return result

    # ================================================================ users
    async def ensure_user(self, user_id: int) -> None:
        """Создаёт пользователя, если его ещё нет."""
        async with self.db.session_factory() as session:
            if await session.get(User, user_id) is None:
                session.add(User(id=user_id))
                try:
                    await session.commit()
                    logger.info("Новый пользователь: %s", user_id)
                except IntegrityError:
                    await session.rollback()

    # ================================================================ accounts
    async def add_account(
        self,
        user_id: int,
        creds: MailCredentials,
        provider: str,
        last_uid: int = 0,
        uid_validity: int = 0,
    ) -> AccountInfo:
        """Сохраняет новый ящик (данные шифруются)."""
        await self.ensure_user(user_id)
        index = self.cipher.blind_index(user_id, creds.email)
        async with self.db.session_factory() as session:
            exists = await session.scalar(
                select(MailAccount.id).where(MailAccount.user_id == user_id, MailAccount.email_index == index)
            )
            if exists:
                raise DuplicateAccountError(creds.email)
            acc = MailAccount(
                user_id=user_id,
                credentials_enc=self.cipher.encrypt_json(user_id, creds.to_dict()),
                title_enc=self.cipher.encrypt(user_id, creds.email),
                email_index=index,
                provider=provider,
                last_uid=last_uid,
                uid_validity=uid_validity,
            )
            session.add(acc)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateAccountError(creds.email) from exc
            await session.refresh(acc)
            logger.info("Пользователь %s добавил ящик id=%s (%s)", user_id, acc.id, provider)
            return self._to_info(acc)

    async def list_accounts(self, user_id: int) -> list[AccountInfo]:
        """Все ящики пользователя."""
        async with self.db.session_factory() as session:
            rows = (
                await session.scalars(
                    select(MailAccount).where(MailAccount.user_id == user_id).order_by(MailAccount.id)
                )
            ).all()
            return self._safe_infos(list(rows))

    async def get_account(self, user_id: int, account_id: int) -> AccountInfo | None:
        """Ящик по id с проверкой владельца (чужой ящик вернёт None)."""
        async with self.db.session_factory() as session:
            acc = await session.get(MailAccount, account_id)
            if acc is None or acc.user_id != user_id:
                return None
            return self._to_info(acc)

    async def get_account_by_id(self, account_id: int) -> AccountInfo | None:
        """Ящик по id без проверки владельца (для фоновых задач)."""
        async with self.db.session_factory() as session:
            acc = await session.get(MailAccount, account_id)
            return self._to_info(acc) if acc else None

    async def delete_account(self, user_id: int, account_id: int) -> bool:
        """Удаляет ящик пользователя вместе со ссылками на письма."""
        async with self.db.session_factory() as session:
            await session.execute(delete(MailMessageRef).where(MailMessageRef.account_id == account_id))
            result = await session.execute(
                delete(MailAccount).where(MailAccount.id == account_id, MailAccount.user_id == user_id)
            )
            await session.commit()
            deleted = result.rowcount > 0
            if deleted:
                logger.info("Пользователь %s удалил ящик id=%s", user_id, account_id)
            return deleted

    async def set_notify(self, user_id: int, account_id: int, notify: bool) -> None:
        """Включает/выключает уведомления о новых письмах (и сбрасывает ошибки)."""
        async with self.db.session_factory() as session:
            acc = await session.get(MailAccount, account_id)
            if acc and acc.user_id == user_id:
                acc.notify = notify
                acc.is_active = True
                acc.error_count = 0
                acc.last_error = None
                await session.commit()
                logger.info("Ящик id=%s: уведомления %s", account_id, "вкл" if notify else "выкл")

    async def update_credentials(self, user_id: int, account_id: int, creds: MailCredentials) -> None:
        """Обновляет учётные данные (например, новый пароль)."""
        async with self.db.session_factory() as session:
            acc = await session.get(MailAccount, account_id)
            if acc and acc.user_id == user_id:
                acc.credentials_enc = self.cipher.encrypt_json(user_id, creds.to_dict())
                acc.is_active = True
                acc.error_count = 0
                acc.last_error = None
                await session.commit()

    async def accounts_for_polling(self) -> list[AccountInfo]:
        """Все ящики, которые нужно проверять на новые письма."""
        async with self.db.session_factory() as session:
            rows = (
                await session.scalars(
                    select(MailAccount).where(MailAccount.notify.is_(True), MailAccount.is_active.is_(True))
                )
            ).all()
            return self._safe_infos(list(rows))

    async def save_poll_state(
        self,
        account_id: int,
        *,
        last_uid: int | None = None,
        uid_validity: int | None = None,
        error: str | None = None,
        max_errors: int = 10,
    ) -> bool:
        """
        Сохраняет результат проверки ящика.

        Если `error` задан — увеличивает счётчик ошибок; при превышении
        `max_errors` ящик деактивируется. Возвращает True, если ящик
        только что был деактивирован.
        """
        deactivated = False
        async with self.db.session_factory() as session:
            acc = await session.get(MailAccount, account_id)
            if acc is None:
                return False
            acc.last_checked_at = datetime.now(timezone.utc)
            if last_uid is not None:
                acc.last_uid = last_uid
            if uid_validity is not None:
                acc.uid_validity = uid_validity
            if error:
                acc.error_count += 1
                acc.last_error = error[:500]
                if acc.error_count >= max_errors and acc.is_active:
                    acc.is_active = False
                    deactivated = True
            else:
                acc.error_count = 0
                acc.last_error = None
            await session.commit()
        return deactivated

    # ================================================================ messages
    async def save_message(self, account: AccountInfo, uid: int, uid_validity: int, meta: dict) -> int:
        """Сохраняет (или находит) ссылку на письмо и возвращает её id."""
        async with self.db.session_factory() as session:
            existing = await session.scalar(
                select(MailMessageRef).where(
                    MailMessageRef.account_id == account.id,
                    MailMessageRef.uid == uid,
                    MailMessageRef.uid_validity == uid_validity,
                )
            )
            enc = self.cipher.encrypt_json(account.user_id, meta)
            if existing:
                existing.meta_enc = enc
                await session.commit()
                return existing.id
            ref = MailMessageRef(account_id=account.id, uid=uid, uid_validity=uid_validity, meta_enc=enc)
            session.add(ref)
            await session.commit()
            await session.refresh(ref)
            return ref.id

    async def get_message(self, user_id: int, message_id: int) -> tuple[MessageInfo, AccountInfo] | None:
        """Ссылка на письмо + ящик, с проверкой, что ящик принадлежит пользователю."""
        async with self.db.session_factory() as session:
            ref = await session.get(MailMessageRef, message_id)
            if ref is None:
                return None
            acc = await session.get(MailAccount, ref.account_id)
            if acc is None or acc.user_id != user_id:
                return None
            info = MessageInfo(
                id=ref.id,
                account_id=ref.account_id,
                uid=ref.uid,
                uid_validity=ref.uid_validity,
                meta=self.cipher.decrypt_json(user_id, ref.meta_enc),
            )
            return info, self._to_info(acc)
