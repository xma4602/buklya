"""
Просмотр почты: список последних писем, открытие письма, отметка «прочитано».
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from buklya.config import Settings
from buklya.db.repository import AccountInfo, Repository
from buklya.services.mail_service import MailService
from buklya.telegram import keyboards as kb
from buklya.telegram.callbacks import AccountCb, MsgCb, OpenCb, PageCb
from buklya.telegram.render import send_email
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)
router = Router(name="inbox")


async def _show_page(
    target: Message,
    acc: AccountInfo,
    offset: int,
    mail: MailService,
    settings: Settings,
    edit: bool,
) -> None:
    """Показывает (или обновляет) страницу списка писем."""
    limit = settings.last_messages_count
    if not edit:
        target = await target.answer(t("inbox.loading", title=acc.title))
    try:
        headers, total, _ = await mail.recent(acc.credentials, offset, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось получить список писем: %s", acc.id, exc)
        await target.edit_text(t("inbox.load_failed", title=acc.title, error=str(exc)[:300]))
        return

    if total == 0:
        await target.edit_text(t("inbox.empty", title=acc.title))
        return

    first, last = offset + 1, min(offset + limit, total)
    text = t("inbox.page", title=acc.title, first=first, last=last, total=total)
    markup = kb.messages_page(acc.id, headers, offset, limit, total)
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # «message is not modified» при нажатии 🔄 без изменений — не ошибка
        if "not modified" not in str(exc):
            raise


@router.message(Command("last"))
@router.message(F.text == kb.BTN_LAST)
async def last_messages(message: Message, state: FSMContext, repo: Repository, mail: MailService, settings: Settings, user_id: int) -> None:
    """Последние письма: сразу список, если ящик один, иначе выбор ящика."""
    await state.clear()
    accounts = await repo.list_accounts(user_id)
    if not accounts:
        await message.answer(t("common.need_account"))
        return
    if len(accounts) == 1:
        await _show_page(message, accounts[0], 0, mail, settings, edit=False)
        return
    await message.answer(t("inbox.choose_account"), reply_markup=kb.choose_account(accounts, "last"))


@router.callback_query(AccountCb.filter(F.action == "last"))
async def last_for_account(
    call: CallbackQuery, callback_data: AccountCb, repo: Repository, mail: MailService, settings: Settings,
    user_id: int,
) -> None:
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(t("inbox.loading", title=acc.title))
    await _show_page(call.message, acc, 0, mail, settings, edit=True)


@router.callback_query(PageCb.filter())
async def page(call: CallbackQuery, callback_data: PageCb, repo: Repository, mail: MailService, settings: Settings, user_id: int) -> None:
    """Листание списка писем."""
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    await call.answer(t.plain("common.loading"))
    await _show_page(call.message, acc, callback_data.offset, mail, settings, edit=True)


@router.callback_query(OpenCb.filter())
async def open_message(
    call: CallbackQuery,
    callback_data: OpenCb,
    bot: Bot,
    repo: Repository,
    mail: MailService,
    settings: Settings,
    user_id: int,
) -> None:
    """Открывает письмо целиком."""
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    await call.answer(t.plain("inbox.opening"))
    try:
        email = await mail.fetch(acc.credentials, callback_data.uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось открыть письмо uid=%s: %s", acc.id, callback_data.uid, exc)
        await call.message.answer(t("inbox.open_failed", error=str(exc)[:300]))
        return
    if email is None:
        await call.message.answer(t("inbox.not_found"))
        return

    ref_id = await repo.save_message(acc, email.uid, acc.uid_validity, email.reply_meta())
    logger.info("user=%s открыл письмо uid=%s из ящика id=%s", user_id, email.uid, acc.id)
    await send_email(
        bot,
        call.from_user.id,
        account_title=acc.title,
        email=email,
        ref_id=ref_id,
        title=t.plain("email.opened"),
        attach_html=settings.attach_html_original,
    )


@router.callback_query(MsgCb.filter(F.action == "seen"))
async def mark_seen(call: CallbackQuery, callback_data: MsgCb, repo: Repository, mail: MailService, user_id: int) -> None:
    """Помечает письмо прочитанным на сервере."""
    found = await repo.get_message(user_id, callback_data.ref_id)
    if found is None:
        await call.answer(t.plain("common.message_not_found"), show_alert=True)
        return
    ref, acc = found
    try:
        await mail.mark_seen(acc.credentials, ref.uid)
    except Exception as exc:  # noqa: BLE001
        await call.answer(t.plain("common.error", error=str(exc)[:150]), show_alert=True)
        return
    await call.answer(t.plain("inbox.marked_seen"))
