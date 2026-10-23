"""
Контекст обработки одного события ВКонтакте.

Обработчики получают `Ctx` — в нём всё нужное: зависимости приложения,
кто пишет, состояние диалога и методы ответа. Ответы передаются как HTML
из файла локализации; перевод в текст + format_data делается здесь.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from buklya.config import Settings
from buklya.db.repository import Repository
from buklya.mail.providers import ProviderRegistry
from buklya.services.linking import LinkService
from buklya.services.mail_service import MailService
from buklya.vk import PLATFORM
from buklya.vk.api import VkGateway
from buklya.vk.markup import html_to_vk
from buklya.vk.states import DialogState, StateStore

logger = logging.getLogger(__name__)


@dataclass
class VkDeps:
    """Зависимости, общие для всех обработчиков."""

    api: VkGateway
    repo: Repository
    mail: MailService
    settings: Settings
    providers: ProviderRegistry
    #: ID пользователей ВКонтакте, которым разрешено пользоваться ботом (пусто — всем)
    allowed_users: list[int] = field(default_factory=list)
    states: StateStore = field(default_factory=StateStore)
    linking: LinkService | None = None

    def __post_init__(self) -> None:
        if self.linking is None:
            self.linking = LinkService(self.repo, self.settings.link_code_ttl_minutes)


def parse_payload(value: Any) -> dict[str, Any]:
    """payload приходит строкой JSON (message_new) или объектом (message_event)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


@dataclass
class Incoming:
    """Входящее сообщение (событие message_new), только нужные поля."""

    peer_id: int
    from_id: int
    text: str
    cmid: int
    payload: dict[str, Any]
    attachments: list[dict[str, Any]]
    format_data: dict[str, Any] | None

    @classmethod
    def from_event(cls, obj: dict[str, Any]) -> "Incoming":
        """Из `object` события message_new (в нём ключ message)."""
        msg = obj.get("message", obj)
        return cls(
            peer_id=int(msg.get("peer_id") or 0),
            from_id=int(msg.get("from_id") or 0),
            text=msg.get("text") or "",
            cmid=int(msg.get("conversation_message_id") or 0),
            payload=parse_payload(msg.get("payload")),
            attachments=list(msg.get("attachments") or []),
            format_data=msg.get("format_data") if isinstance(msg.get("format_data"), dict) else None,
        )


@dataclass
class ButtonEvent:
    """Нажатие callback-кнопки (событие message_event)."""

    event_id: str
    user_id: int
    peer_id: int
    cmid: int
    payload: dict[str, Any]

    @classmethod
    def from_event(cls, obj: dict[str, Any]) -> "ButtonEvent":
        return cls(
            event_id=str(obj.get("event_id") or ""),
            user_id=int(obj.get("user_id") or 0),
            peer_id=int(obj.get("peer_id") or 0),
            cmid=int(obj.get("conversation_message_id") or 0),
            payload=parse_payload(obj.get("payload")),
        )


class Ctx:
    """Контекст одного события: пользователь, состояние диалога и ответы."""

    def __init__(
        self,
        deps: VkDeps,
        vk_user_id: int,
        peer_id: int,
        message: Incoming | None = None,
        event: ButtonEvent | None = None,
    ) -> None:
        self.deps = deps
        self.vk_user_id = vk_user_id
        self.peer_id = peer_id
        self.message = message
        self.event = event
        #: ID пользователя Букли — заполняется диспетчером (`resolve_user`) до вызова обработчика
        self.user_id: int = 0
        #: Состояние диалога этого чата ВКонтакте (черновики и мастера — у каждого мессенджера свои)
        self.state: DialogState = deps.states.get(vk_user_id)
        self._event_answered = False

    async def resolve_user(self) -> int:
        """Находит (или создаёт) пользователя Букли по аккаунту ВКонтакте."""
        self.user_id = await self.deps.repo.resolve_user(PLATFORM, self.vk_user_id)
        return self.user_id

    # ------------------------------------------------------------------ короткие ссылки
    @property
    def api(self) -> VkGateway:
        return self.deps.api

    @property
    def repo(self) -> Repository:
        return self.deps.repo

    @property
    def mail(self) -> MailService:
        return self.deps.mail

    @property
    def settings(self) -> Settings:
        return self.deps.settings

    @property
    def linking(self) -> LinkService:
        return self.deps.linking

    @property
    def text(self) -> str:
        """Текст входящего сообщения (без пробелов по краям)."""
        return (self.message.text if self.message else "").strip()

    @property
    def payload(self) -> dict[str, Any]:
        source = self.event or self.message
        return source.payload if source else {}

    # ------------------------------------------------------------------ ответы
    async def send(self, html: str, keyboard: str | None = None, attachment: str | None = None) -> int:
        """Отправляет сообщение (HTML из локализации). Возвращает cmid."""
        return await self.api.send(self.peer_id, html_to_vk(html), keyboard=keyboard, attachment=attachment)

    async def edit(self, cmid: int, html: str, keyboard: str | None = None) -> None:
        """Редактирует сообщение бота; если не вышло (старое сообщение) — отправляет новое."""
        if cmid:
            try:
                await self.api.edit(self.peer_id, cmid, html_to_vk(html), keyboard=keyboard)
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось отредактировать cmid=%s: %s — отправляю новое", cmid, exc)
        await self.send(html, keyboard)

    async def edit_current(self, html: str, keyboard: str | None = None) -> None:
        """Редактирует сообщение, под которым нажата кнопка."""
        await self.edit(self.event.cmid if self.event else 0, html, keyboard)

    async def answer(self, text: str | None = None) -> None:
        """Отвечает на нажатие кнопки (обязательно, иначе у пользователя «крутится» кнопка)."""
        if self.event is None or self._event_answered:
            return
        self._event_answered = True
        await self.api.answer_event(self.event.event_id, self.event.user_id, self.event.peer_id, text)
