"""
Связка мессенджеров: один пользователь Букли в Telegram, ВКонтакте и т.д.

Как это выглядит для пользователя:
    1. В одном мессенджере: /link -> бот показывает одноразовый код (действует несколько минут).
    2. В другом мессенджере: /link КОД -> профили объединяются, ящики становятся общими.

Код в базе хранится только в виде HMAC и гасится при первом использовании.
Логика здесь общая — платформы только показывают тексты и кнопки.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from buklya.db.repository import IdentityInfo, MergeResult, Repository
from buklya.i18n import Safe, Translator

logger = logging.getLogger(__name__)

#: Символы кода: без похожих друг на друга (0/O, 1/I/L)
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
_CODE_RE = re.compile(r"[^A-Z0-9]")


def normalize_code(text: str) -> str:
    """Код в том виде, как его ввёл пользователь -> канонический (без пробелов/дефисов, заглавными)."""
    return _CODE_RE.sub("", (text or "").upper())


def format_code(code: str) -> str:
    """ABCD2345 -> ABCD-2345 (так удобнее читать и переписывать)."""
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LENGTH else code


@dataclass
class LinkResult:
    """Итог ввода кода."""

    #: ok | invalid (нет такого кода / истёк) | same (уже связаны)
    status: str
    merge: MergeResult | None = None
    user_id: int | None = None


class LinkService:
    """Выдача и погашение кодов связки, отвязка мессенджера."""

    def __init__(self, repo: Repository, ttl_minutes: int = 10) -> None:
        self.repo = repo
        self.ttl = timedelta(minutes=ttl_minutes)

    async def issue_code(self, user_id: int) -> tuple[str, datetime]:
        """Новый код для пользователя (предыдущий код пользователя перестаёт действовать)."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = await self.repo.create_link_code(user_id, code, self.ttl)
        logger.info("Пользователь id=%s получил код связки", user_id)
        return format_code(code), expires

    async def redeem(self, platform: str, external_id: str | int, text: str) -> LinkResult:
        """
        Пользователь ввёл код в мессенджере `platform`: его текущий профиль
        объединяется с профилем владельца кода (ящики переносятся туда).
        """
        code = normalize_code(text)
        if len(code) != CODE_LENGTH:
            return LinkResult("invalid")
        owner = await self.repo.take_link_code(code)
        if owner is None:
            return LinkResult("invalid")
        current = await self.repo.resolve_user(platform, external_id)
        if current == owner:
            return LinkResult("same", user_id=owner)
        merge = await self.repo.merge_users(current, owner)
        return LinkResult("ok", merge=merge, user_id=owner)

    async def unlink(self, platform: str, external_id: str | int) -> int:
        """Отвязывает мессенджер: он получает новый пустой профиль. Возвращает его ID."""
        return await self.repo.detach_identity(platform, external_id)

    async def identities(self, user_id: int) -> list[IdentityInfo]:
        return await self.repo.list_identities(user_id)


# ============================================================================ тексты (общие для платформ)
def platforms_list(tr: Translator, identities: list[IdentityInfo]) -> str:
    """«Telegram, ВКонтакте» — названия связанных мессенджеров."""
    names: list[str] = []
    for identity in identities:
        key = f"platforms.{identity.platform}"
        name = tr.plain(key) if tr.has(key) else identity.platform
        if name not in names:
            names.append(name)
    return ", ".join(names)


def screen_text(tr: Translator, identities: list[IdentityInfo], code: str, ttl_minutes: int) -> Safe:
    """Экран «Мессенджеры»: что связано и код для связки."""
    return Safe(
        tr("link.title") + "\n"
        + tr("link.linked", list=platforms_list(tr, identities)) + "\n\n"
        + tr("link.code", code=code, minutes=ttl_minutes)
    )


def done_text(tr: Translator, result: LinkResult, identities: list[IdentityInfo], accounts: int) -> Safe:
    """Ответ на ввод кода."""
    if result.status == "invalid":
        return tr("link.invalid")
    if result.status == "same":
        return tr("link.same")
    text = tr("link.done", list=platforms_list(tr, identities), count=accounts)
    if result.merge and result.merge.duplicate_accounts:
        text += "\n" + tr("link.done_duplicates", count=result.merge.duplicate_accounts)
    return Safe(text)
