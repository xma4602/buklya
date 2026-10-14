"""
Общие команды: /start, /help, /cancel.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from buklya.db.repository import Repository
from buklya.telegram.keyboards import BTN_HELP, main_menu

logger = logging.getLogger(__name__)
router = Router(name="common")

HELP_TEXT = """\
🦉 <b>Букля</b> — почтовая сова в Telegram.

<b>Что я умею</b>
• присылать новые письма из ваших ящиков (с указанием ящика и вложениями);
• отвечать на письма прямо из чата — кнопка «↩️ Ответить» под письмом;
• писать новые письма — «✉️ Написать»;
• показывать последние письма — «📬 Последние письма», письмо открывается по нажатию.

<b>Как написать ответ</b>
Нажмите «Ответить» и отправляйте сообщения: текст (жирный, курсив, ссылки,
код, цитаты сохранятся), фото, документы, голосовые. Письмо можно набирать
<b>несколькими сообщениями</b> — всё соберётся в один черновик.
Когда готово — «📤 Отправить».

<b>Подключение ящика</b>
«➕ Добавить ящик» → адрес → пароль. Для Яндекса, Gmail и Mail.ru нужен
<b>пароль приложения</b> (бот подскажет, где его взять).
Пароли хранятся только в зашифрованном виде, а сообщение с паролем бот сразу удаляет.

<b>Команды</b>
/add — подключить ящик
/accounts — мои ящики
/last — последние письма
/compose — новое письмо
/cancel — отменить текущее действие
/help — эта справка
"""


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, repo: Repository) -> None:
    """Приветствие и регистрация пользователя."""
    await state.clear()
    await repo.ensure_user(message.from_user.id)
    accounts = await repo.list_accounts(message.from_user.id)
    text = (
        f"Привет, {message.from_user.first_name or 'друг'}! 🦉\n\n"
        "Я — Букля, буду приносить вашу почту прямо сюда.\n"
    )
    if not accounts:
        text += "\nДля начала подключите почтовый ящик: нажмите «➕ Добавить ящик» или /add."
    else:
        text += f"\nПодключено ящиков: {len(accounts)}. Новые письма придут автоматически."
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    """Справка."""
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отмена любого диалога (подключение ящика, написание письма и т.п.)."""
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("Действие отменено.", reply_markup=main_menu())
    else:
        await message.answer("Отменять нечего 🙂", reply_markup=main_menu())
