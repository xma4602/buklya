"""
Связка мессенджеров (Telegram ↔ ВКонтакте и др.): общие ящики для одного человека.

/link        — показать связанные мессенджеры и код для связки;
/link КОД    — ввести код, полученный в другом мессенджере.
Логика — в `buklya.services.linking`, здесь только интерфейс Telegram.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from buklya.db.repository import Repository
from buklya.services import linking as lk
from buklya.services.linking import LinkService
from buklya.telegram import PLATFORM
from buklya.telegram import keyboards as kb
from buklya.telegram.callbacks import LinkCb
from buklya.telegram.states import LinkMessengers
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)
router = Router(name="link")


async def _screen(linking: LinkService, user_id: int) -> tuple[str, object]:
    """Текст и кнопки экрана «Мессенджеры» (каждый показ — новый код)."""
    identities = await linking.identities(user_id)
    code, _ = await linking.issue_code(user_id)
    text = lk.screen_text(t, identities, code, int(linking.ttl.total_seconds() // 60))
    return text, kb.link_menu(can_unlink=len(identities) > 1)


async def _redeem(message: Message, state: FSMContext, repo: Repository, linking: LinkService, code: str) -> None:
    await state.clear()
    result = await linking.redeem(PLATFORM, message.from_user.id, code)
    if result.status == "ok":
        logger.info("Telegram %s связан с пользователем id=%s", message.from_user.id, result.user_id)
    identities = await linking.identities(result.user_id) if result.user_id else []
    accounts = len(await repo.list_accounts(result.user_id)) if result.user_id else 0
    await message.answer(lk.done_text(t, result, identities, accounts), reply_markup=kb.main_menu())


@router.message(Command("link"))
@router.message(F.text == kb.BTN_LINK)
async def link_start(
    message: Message, state: FSMContext, repo: Repository, linking: LinkService, user_id: int,
    command: CommandObject | None = None,
) -> None:
    """Экран «Мессенджеры» или сразу ввод кода (/link КОД)."""
    if command is not None and command.args:
        await _redeem(message, state, repo, linking, command.args)
        return
    await state.clear()
    text, markup = await _screen(linking, user_id)
    await message.answer(text, reply_markup=markup)


@router.message(LinkMessengers.code, F.text)
async def link_code(message: Message, state: FSMContext, repo: Repository, linking: LinkService) -> None:
    await _redeem(message, state, repo, linking, message.text)


@router.callback_query(LinkCb.filter())
async def link_buttons(
    call: CallbackQuery, callback_data: LinkCb, state: FSMContext, linking: LinkService, user_id: int
) -> None:
    action = callback_data.action
    if action == "cancel":
        await call.answer()
        await state.clear()
        await call.message.edit_text(t("common.cancelled"))
    elif action == "enter":
        await call.answer()
        await state.set_state(LinkMessengers.code)
        await call.message.answer(t("link.ask_code"), reply_markup=kb.link_cancel())
    elif action == "code":
        await call.answer()
        text, markup = await _screen(linking, user_id)
        await call.message.edit_text(text, reply_markup=markup)
    elif action == "unlink":
        identities = await linking.identities(user_id)
        if len(identities) < 2:
            await call.answer(t.plain("link.nothing_to_unlink"), show_alert=True)
            return
        await call.answer()
        await state.clear()
        await linking.unlink(PLATFORM, call.from_user.id)
        rest = [i for i in identities if not (i.platform == PLATFORM and i.external_id == str(call.from_user.id))]
        await call.message.edit_text(t("link.unlinked", list=lk.platforms_list(t, rest)))
