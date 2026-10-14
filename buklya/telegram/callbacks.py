"""
Фабрики callback_data для inline-кнопок.

Telegram ограничивает callback_data 64 байтами, поэтому в кнопки кладутся
только короткие идентификаторы (id ящика, UID письма, id ссылки на письмо).
"""

from aiogram.filters.callback_data import CallbackData


class AccountCb(CallbackData, prefix="acc"):
    """Действия с ящиком: menu, toggle, last, compose, passwd, delete, delete_ok, list."""

    action: str
    account_id: int = 0


class PageCb(CallbackData, prefix="pg"):
    """Страница списка последних писем ящика."""

    account_id: int
    offset: int = 0


class OpenCb(CallbackData, prefix="op"):
    """Открыть письмо по UID."""

    account_id: int
    uid: int


class MsgCb(CallbackData, prefix="msg"):
    """Действия с показанным письмом: reply, replyall, seen."""

    action: str
    ref_id: int


class ComposeCb(CallbackData, prefix="cmp"):
    """Кнопки черновика: send, preview, clear, cancel, subject, cc."""

    action: str


class AddCb(CallbackData, prefix="add"):
    """Кнопки мастера подключения ящика: manual, password, login, cancel."""

    action: str
