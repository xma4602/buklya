"""
Клавиатуры бота (reply и inline).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from buklya.db.repository import AccountInfo
from buklya.mail.models import MessageHeader
from buklya.telegram.callbacks import AccountCb, AddCb, ComposeCb, MsgCb, OpenCb, PageCb

# Тексты кнопок главного меню (используются и в фильтрах хендлеров)
BTN_LAST = "📬 Последние письма"
BTN_COMPOSE = "✉️ Написать"
BTN_ACCOUNTS = "📮 Мои ящики"
BTN_ADD = "➕ Добавить ящик"
BTN_HELP = "❓ Помощь"


def main_menu() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LAST), KeyboardButton(text=BTN_COMPOSE)],
            [KeyboardButton(text=BTN_ACCOUNTS), KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def account_status_icon(acc: AccountInfo) -> str:
    """Иконка состояния ящика."""
    if not acc.is_active:
        return "⚠️"
    return "🔔" if acc.notify else "🔕"


def accounts_list(accounts: list[AccountInfo]) -> InlineKeyboardMarkup:
    """Список ящиков пользователя."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(text=f"{account_status_icon(acc)} {acc.title}", callback_data=AccountCb(action="menu", account_id=acc.id))
    kb.button(text=BTN_ADD, callback_data=AccountCb(action="add"))
    kb.adjust(1)
    return kb.as_markup()


def account_menu(acc: AccountInfo) -> InlineKeyboardMarkup:
    """Меню одного ящика."""
    kb = InlineKeyboardBuilder()
    toggle_text = "🔕 Выключить уведомления" if acc.notify and acc.is_active else "🔔 Включить уведомления"
    kb.button(text=toggle_text, callback_data=AccountCb(action="toggle", account_id=acc.id))
    kb.button(text=BTN_LAST, callback_data=AccountCb(action="last", account_id=acc.id))
    kb.button(text=BTN_COMPOSE, callback_data=AccountCb(action="compose", account_id=acc.id))
    kb.button(text="🔑 Сменить пароль", callback_data=AccountCb(action="passwd", account_id=acc.id))
    kb.button(text="🗑 Удалить ящик", callback_data=AccountCb(action="delete", account_id=acc.id))
    kb.button(text="« К списку ящиков", callback_data=AccountCb(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_delete(account_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления ящика."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=AccountCb(action="delete_ok", account_id=account_id))
    kb.button(text="Отмена", callback_data=AccountCb(action="menu", account_id=account_id))
    kb.adjust(2)
    return kb.as_markup()


def choose_account(accounts: list[AccountInfo], action: str) -> InlineKeyboardMarkup:
    """Выбор ящика для действия (last / compose)."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(text=f"📮 {acc.title}", callback_data=AccountCb(action=action, account_id=acc.id))
    kb.adjust(1)
    return kb.as_markup()


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def messages_page(account_id: int, headers: list[MessageHeader], offset: int, limit: int, total: int) -> InlineKeyboardMarkup:
    """Список последних писем: по кнопке на письмо + навигация."""
    kb = InlineKeyboardBuilder()
    for h in headers:
        mark = "🔵 " if not h.seen else ""
        label = f"{mark}{_short(h.from_, 20)} — {_short(h.subject, 40)}"
        kb.row(_btn(label, OpenCb(account_id=account_id, uid=h.uid)))

    nav = []
    if offset > 0:
        nav.append(_btn("◀️ Новее", PageCb(account_id=account_id, offset=max(offset - limit, 0))))
    nav.append(_btn("🔄", PageCb(account_id=account_id, offset=offset)))
    if offset + limit < total:
        nav.append(_btn("Старее ▶️", PageCb(account_id=account_id, offset=offset + limit)))
    kb.row(*nav)
    return kb.as_markup()


def _btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    """Inline-кнопка с упакованным callback_data."""
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def email_actions(ref_id: int, has_multiple_recipients: bool = True) -> InlineKeyboardMarkup:
    """Кнопки под показанным письмом."""
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Ответить", callback_data=MsgCb(action="reply", ref_id=ref_id))
    if has_multiple_recipients:
        kb.button(text="↩️ Ответить всем", callback_data=MsgCb(action="replyall", ref_id=ref_id))
    kb.button(text="✅ Прочитано", callback_data=MsgCb(action="seen", ref_id=ref_id))
    if has_multiple_recipients:
        kb.adjust(2, 1)
    else:
        kb.adjust(2)
    return kb.as_markup()


def compose_panel() -> InlineKeyboardMarkup:
    """Панель черновика письма."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Отправить", callback_data=ComposeCb(action="send"))
    kb.button(text="👀 Предпросмотр", callback_data=ComposeCb(action="preview"))
    kb.button(text="✏️ Тема", callback_data=ComposeCb(action="subject"))
    kb.button(text="👥 Копия", callback_data=ComposeCb(action="cc"))
    kb.button(text="🧹 Очистить", callback_data=ComposeCb(action="clear"))
    kb.button(text="❌ Отмена", callback_data=ComposeCb(action="cancel"))
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def compose_cancel() -> InlineKeyboardMarkup:
    """Одна кнопка отмены написания письма."""
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ComposeCb(action="cancel"))
    return kb.as_markup()


def add_cancel() -> InlineKeyboardMarkup:
    """Кнопка отмены мастера подключения."""
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AddCb(action="cancel"))
    return kb.as_markup()


def add_failed() -> InlineKeyboardMarkup:
    """Варианты после неудачной попытки подключения."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Ввести пароль ещё раз", callback_data=AddCb(action="password"))
    kb.button(text="👤 Логин отличается от адреса", callback_data=AddCb(action="login"))
    kb.button(text="⚙️ Указать серверы вручную", callback_data=AddCb(action="manual"))
    kb.button(text="❌ Отмена", callback_data=AddCb(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()
