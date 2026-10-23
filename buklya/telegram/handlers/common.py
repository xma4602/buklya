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
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)
router = Router(name="common")

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, repo: Repository, user_id: int) -> None:
    """Приветствие и регистрация пользователя."""
    await state.clear()
    accounts = await repo.list_accounts(user_id)
    text = t("common.start", name=message.from_user.first_name or t.plain("common.friend")) + "\n\n"
    if not accounts:
        text += t("common.start_no_accounts")
    else:
        text += t("common.start_has_accounts", count=len(accounts))
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    """Справка."""
    await message.answer(t("help.text"), reply_markup=main_menu())


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отмена любого диалога (подключение ящика, написание письма и т.п.)."""
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(t("common.cancelled"), reply_markup=main_menu())
    else:
        await message.answer(t("common.nothing_to_cancel"), reply_markup=main_menu())
