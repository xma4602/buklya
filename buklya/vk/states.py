"""
Состояния диалогов бота ВКонтакте.

Хранилище в памяти процесса (как MemoryStorage у Telegram-бота): при перезапуске
незавершённые диалоги и черновики теряются.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Имена состояний (по смыслу совпадают с buklya.telegram.states)
ADD_EMAIL = "add:email"  # мастер подключения: ждём адрес
ADD_PASSWORD = "add:password"  # ждём пароль / пароль приложения
ADD_LOGIN = "add:login"  # ждём логин, если он отличается от адреса
ADD_IMAP = "add:imap"  # ручной ввод IMAP-сервера host:port
ADD_SMTP = "add:smtp"  # ручной ввод SMTP-сервера host:port[:ssl|starttls]
CHANGE_PASSWORD = "passwd"  # смена пароля подключённого ящика
COMPOSE_TO = "compose:to"  # адресаты нового письма
COMPOSE_SUBJECT = "compose:subject"  # тема нового письма
COMPOSE_BODY = "compose:body"  # текст и вложения (можно несколькими сообщениями)
COMPOSE_EDIT_SUBJECT = "compose:edit_subject"  # смена темы из черновика
COMPOSE_EDIT_CC = "compose:edit_cc"  # копия из черновика
LINK_CODE = "link:code"  # ждём код связки из другого мессенджера


@dataclass
class DialogState:
    """Состояние диалога одного пользователя: имя шага и накопленные данные."""

    name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def clear(self) -> None:
        self.name = None
        self.data = {}

    def set(self, name: str | None) -> None:
        self.name = name

    def update(self, **values: Any) -> None:
        self.data.update(values)

    def in_group(self, prefix: str) -> bool:
        """Состояние относится к группе (например, все шаги «compose:»)."""
        return bool(self.name and self.name.startswith(prefix))


class StateStore:
    """Состояния и блокировки по пользователям ВКонтакте (ключ — ID ВКонтакте)."""

    def __init__(self) -> None:
        self._states: dict[int, DialogState] = defaultdict(DialogState)
        # События одного пользователя обрабатываются по очереди — иначе быстро
        # отправленные сообщения могли бы потерять обновления черновика.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def get(self, user_id: int) -> DialogState:
        return self._states[user_id]

    def lock(self, user_id: int) -> asyncio.Lock:
        return self._locks[user_id]
