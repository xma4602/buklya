"""
Репозиторий — единственное место, где код работает с БД напрямую.

Остальные части приложения получают уже расшифрованные объекты
(`AccountInfo`, `MessageInfo`, `IdentityInfo`) и не знают ни о сессиях, ни о шифровании.

Пользователь Букли (`users.id`) один на всех мессенджерах: обработчики сначала
находят его по аккаунту мессенджера (`resolve_user`), а дальше работают с его ящиками.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from buklya.crypto import CredentialCipher, CryptoError
from buklya.db.database import Database
from buklya.db.models import DeliveryCursor, Identity, LinkCode, MailAccount, MailMessageRef, User
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


@dataclass
class IdentityInfo:
    """Аккаунт пользователя в мессенджере."""

    id: int
    user_id: int
    platform: str
    external_id: str
    blocked: bool


@dataclass
class CursorInfo:
    """Курсор доставки ящика в платформу."""

    platform: str
    last_uid: int
    uid_validity: int


@dataclass
class MergeResult:
    """Итог слияния пользователей при связке мессенджеров."""

    moved_accounts: int
    #: Ящики, которые были подключены в обоих профилях (лишняя копия удалена)
    duplicate_accounts: int


def _aware(value: datetime) -> datetime:
    """SQLite возвращает datetime без часового пояса — считаем его UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Repository:
    """Операции с данными пользователей, мессенджеров, ящиков и писем."""

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

    @staticmethod
    def _identity_info(row: Identity) -> IdentityInfo:
        return IdentityInfo(row.id, row.user_id, row.platform, row.external_id, row.blocked)

    # ================================================================ пользователи и мессенджеры
    async def resolve_user(self, platform: str, external_id: str | int) -> int:
        """
        ID пользователя Букли по аккаунту мессенджера. Если такого ещё нет — создаёт
        нового пользователя. Заодно снимает отметку «заблокировал бота»: раз пишет — доступен.
        """
        external_id = str(external_id)
        async with self.db.session_factory() as session:
            row = await session.scalar(
                select(Identity).where(Identity.platform == platform, Identity.external_id == external_id)
            )
            if row is not None:
                if row.blocked:
                    row.blocked = False
                    await session.commit()
                return row.user_id
            user = User()
            session.add(user)
            await session.flush()
            session.add(Identity(user_id=user.id, platform=platform, external_id=external_id))
            try:
                await session.commit()
            except IntegrityError:  # тот же пользователь пришёл одновременно с двух событий
                await session.rollback()
                return await self.resolve_user(platform, external_id)
            logger.info("Новый пользователь id=%s (%s)", user.id, platform)
            return user.id

    async def ensure_user(self, user_id: int) -> None:
        """Создаёт пользователя с заданным id, если его ещё нет (для тестов и служебных задач)."""
        async with self.db.session_factory() as session:
            if await session.get(User, user_id) is None:
                session.add(User(id=user_id))
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()

    async def list_identities(self, user_id: int, platform: str | None = None) -> list[IdentityInfo]:
        """Мессенджеры пользователя (все или одной платформы)."""
        query = select(Identity).where(Identity.user_id == user_id).order_by(Identity.id)
        if platform is not None:
            query = query.where(Identity.platform == platform)
        async with self.db.session_factory() as session:
            return [self._identity_info(row) for row in (await session.scalars(query)).all()]

    async def set_identity_blocked(self, platform: str, external_id: str | int, blocked: bool = True) -> None:
        """Отмечает, что в этот мессенджер писать нельзя (пользователь заблокировал бота)."""
        async with self.db.session_factory() as session:
            await session.execute(
                update(Identity)
                .where(Identity.platform == platform, Identity.external_id == str(external_id))
                .values(blocked=blocked)
            )
            await session.commit()

    async def detach_identity(self, platform: str, external_id: str | int) -> int:
        """
        Отвязывает мессенджер от пользователя: он становится новым пустым пользователем.
        Ящики остаются у прежнего пользователя (в его других мессенджерах). Возвращает новый ID.
        """
        async with self.db.session_factory() as session:
            row = await session.scalar(
                select(Identity).where(Identity.platform == platform, Identity.external_id == str(external_id))
            )
            if row is None:
                raise LookupError("identity not found")
            user = User()
            session.add(user)
            await session.flush()
            old_user = row.user_id
            row.user_id = user.id
            await session.commit()
            logger.info("Мессенджер %s отвязан от пользователя id=%s -> id=%s", platform, old_user, user.id)
            return user.id

    # ================================================================ коды связки
    async def create_link_code(self, user_id: int, code: str, ttl: timedelta) -> datetime:
        """Сохраняет одноразовый код (его HMAC) для пользователя; старые коды пользователя удаляются."""
        expires = datetime.now(timezone.utc) + ttl
        async with self.db.session_factory() as session:
            await session.execute(delete(LinkCode).where(LinkCode.user_id == user_id))
            await session.execute(delete(LinkCode).where(LinkCode.expires_at < datetime.now(timezone.utc)))
            session.add(LinkCode(code_hash=self.cipher.token_hash(code), user_id=user_id, expires_at=expires))
            await session.commit()
        return expires

    async def take_link_code(self, code: str) -> int | None:
        """Погашает код: возвращает владельца, если код существует и не истёк (код удаляется)."""
        async with self.db.session_factory() as session:
            row = await session.scalar(select(LinkCode).where(LinkCode.code_hash == self.cipher.token_hash(code)))
            if row is None:
                return None
            await session.delete(row)
            await session.commit()
            if _aware(row.expires_at) < datetime.now(timezone.utc):
                return None
            return row.user_id

    # ================================================================ слияние пользователей
    async def merge_users(self, source_id: int, target_id: int) -> MergeResult:
        """
        Переносит всё от пользователя `source_id` к `target_id` и удаляет `source_id`.

        Данные зашифрованы ключом пользователя, поэтому ящики и метаданные писем
        перешифровываются. Ящик, подключённый у обоих, остаётся в одном экземпляре
        (у `target_id`).
        """
        if source_id == target_id:
            return MergeResult(0, 0)
        moved = duplicates = 0
        async with self.db.session_factory() as session:
            target_indexes = set(
                (await session.scalars(select(MailAccount.email_index).where(MailAccount.user_id == target_id))).all()
            )
            accounts = (await session.scalars(select(MailAccount).where(MailAccount.user_id == source_id))).all()
            for acc in accounts:
                creds = self.cipher.decrypt_json(source_id, acc.credentials_enc)
                title = self.cipher.decrypt(source_id, acc.title_enc)
                new_index = self.cipher.blind_index(target_id, creds["email"])
                if new_index in target_indexes:
                    await session.execute(delete(MailMessageRef).where(MailMessageRef.account_id == acc.id))
                    await session.execute(delete(DeliveryCursor).where(DeliveryCursor.account_id == acc.id))
                    await session.delete(acc)
                    duplicates += 1
                    continue
                refs = (await session.scalars(select(MailMessageRef).where(MailMessageRef.account_id == acc.id))).all()
                for ref in refs:
                    ref.meta_enc = self.cipher.encrypt_json(target_id, self.cipher.decrypt_json(source_id, ref.meta_enc))
                acc.credentials_enc = self.cipher.encrypt_json(target_id, creds)
                acc.title_enc = self.cipher.encrypt(target_id, title)
                acc.email_index = new_index
                acc.user_id = target_id
                target_indexes.add(new_index)
                moved += 1
            await session.execute(update(Identity).where(Identity.user_id == source_id).values(user_id=target_id))
            await session.execute(delete(LinkCode).where(LinkCode.user_id == source_id))
            await session.execute(delete(User).where(User.id == source_id))
            await session.commit()
        await self.ensure_cursors(target_id)
        logger.info("Пользователь id=%s объединён с id=%s (ящиков перенесено: %d, дублей: %d)",
                    source_id, target_id, moved, duplicates)
        return MergeResult(moved, duplicates)

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
            info = self._to_info(acc)
        await self.ensure_cursors(user_id)
        return info

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
        """Удаляет ящик пользователя вместе со ссылками на письма и курсорами."""
        async with self.db.session_factory() as session:
            owner = await session.scalar(select(MailAccount.user_id).where(MailAccount.id == account_id))
            if owner != user_id:
                return False
            await session.execute(delete(MailMessageRef).where(MailMessageRef.account_id == account_id))
            await session.execute(delete(DeliveryCursor).where(DeliveryCursor.account_id == account_id))
            await session.execute(delete(MailAccount).where(MailAccount.id == account_id))
            await session.commit()
            logger.info("Пользователь %s удалил ящик id=%s", user_id, account_id)
            return True

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

    # ================================================================ курсоры доставки
    async def ensure_cursors(self, user_id: int) -> None:
        """
        Создаёт недостающие курсоры доставки: для каждого ящика пользователя и каждой
        платформы его мессенджеров. Новый курсор начинается с последнего обработанного
        письма ящика — в только что привязанный мессенджер старые письма не приходят,
        а процесс, который запустится позже, ничего не пропустит.
        """
        async with self.db.session_factory() as session:
            platforms = set((await session.scalars(
                select(Identity.platform).where(Identity.user_id == user_id))).all())
            accounts = (await session.scalars(select(MailAccount).where(MailAccount.user_id == user_id))).all()
            for acc in accounts:
                existing = set((await session.scalars(
                    select(DeliveryCursor.platform).where(DeliveryCursor.account_id == acc.id))).all())
                for platform in platforms - existing:
                    session.add(DeliveryCursor(account_id=acc.id, platform=platform,
                                               last_uid=acc.last_uid, uid_validity=acc.uid_validity))
            try:
                await session.commit()
            except IntegrityError:  # курсор одновременно создал другой процесс
                await session.rollback()

    # ================================================================ проверка почты
    async def accounts_for_polling(self, platforms: Iterable[str]) -> list[AccountInfo]:
        """
        Ящики, которые нужно проверять процессу с ботами `platforms`: уведомления
        включены, и у владельца есть доступный мессенджер хотя бы одной из этих платформ.
        """
        platforms = list(platforms)
        if not platforms:
            return []
        owners = (
            select(Identity.user_id)
            .where(Identity.platform.in_(platforms), Identity.blocked.is_(False))
            .distinct()
        )
        query = select(MailAccount).where(
            MailAccount.notify.is_(True), MailAccount.is_active.is_(True), MailAccount.user_id.in_(owners)
        )
        async with self.db.session_factory() as session:
            rows = (await session.scalars(query)).all()
            return self._safe_infos(list(rows))

    async def get_cursors(self, account_id: int) -> dict[str, CursorInfo]:
        """Курсоры доставки ящика по платформам."""
        async with self.db.session_factory() as session:
            rows = (await session.scalars(select(DeliveryCursor).where(DeliveryCursor.account_id == account_id))).all()
            return {r.platform: CursorInfo(r.platform, r.last_uid, r.uid_validity) for r in rows}

    async def save_cursor(self, account_id: int, platform: str, last_uid: int, uid_validity: int) -> None:
        """Сохраняет курсор доставки (создаёт при первом сохранении)."""
        async with self.db.session_factory() as session:
            row = await session.scalar(
                select(DeliveryCursor).where(DeliveryCursor.account_id == account_id, DeliveryCursor.platform == platform)
            )
            if row is None:
                session.add(DeliveryCursor(account_id=account_id, platform=platform,
                                           last_uid=last_uid, uid_validity=uid_validity))
            else:
                row.last_uid = last_uid
                row.uid_validity = uid_validity
            try:
                await session.commit()
            except IntegrityError:  # курсор одновременно создал другой процесс
                await session.rollback()
                await self.save_cursor(account_id, platform, last_uid, uid_validity)

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

        `last_uid` только растёт (ящик могут проверять несколько процессов).
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
            if uid_validity is not None and uid_validity != acc.uid_validity:
                acc.uid_validity = uid_validity
                acc.last_uid = last_uid or 0
            elif last_uid is not None:
                acc.last_uid = max(acc.last_uid, last_uid)
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
            try:
                await session.commit()
            except IntegrityError:  # ту же ссылку одновременно сохранил другой процесс
                await session.rollback()
                return await self.save_message(account, uid, uid_validity, meta)
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
