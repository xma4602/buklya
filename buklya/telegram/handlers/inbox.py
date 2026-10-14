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
from buklya.telegram.render import esc, send_email

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
        target = await target.answer(f"⏳ Загружаю письма из <b>{esc(acc.title)}</b>…")
    try:
        headers, total, _ = await mail.recent(acc.credentials, offset, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось получить список писем: %s", acc.id, exc)
        await target.edit_text(f"❌ Не удалось получить письма из <b>{esc(acc.title)}</b>:\n<code>{esc(str(exc)[:300])}</code>")
        return

    if total == 0:
        await target.edit_text(f"📭 Во «Входящих» <b>{esc(acc.title)}</b> пусто.")
        return

    first, last = offset + 1, min(offset + limit, total)
    text = (
        f"📬 <b>{esc(acc.title)}</b> — письма {first}–{last} из {total}\n"
        "🔵 — непрочитанные. Нажмите на письмо, чтобы открыть."
    )
    markup = kb.messages_page(acc.id, headers, offset, limit, total)
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # «message is not modified» при нажатии 🔄 без изменений — не ошибка
        if "not modified" not in str(exc):
            raise


@router.message(Command("last"))
@router.message(F.text == kb.BTN_LAST)
async def last_messages(message: Message, state: FSMContext, repo: Repository, mail: MailService, settings: Settings) -> None:
    """Последние письма: сразу список, если ящик один, иначе выбор ящика."""
    await state.clear()
    accounts = await repo.list_accounts(message.chat.id)
    if not accounts:
        await message.answer("Сначала подключите ящик: /add")
        return
    if len(accounts) == 1:
        await _show_page(message, accounts[0], 0, mail, settings, edit=False)
        return
    await message.answer("Из какого ящика показать письма?", reply_markup=kb.choose_account(accounts, "last"))


@router.callback_query(AccountCb.filter(F.action == "last"))
async def last_for_account(
    call: CallbackQuery, callback_data: AccountCb, repo: Repository, mail: MailService, settings: Settings
) -> None:
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(f"⏳ Загружаю письма из <b>{esc(acc.title)}</b>…")
    await _show_page(call.message, acc, 0, mail, settings, edit=True)


@router.callback_query(PageCb.filter())
async def page(call: CallbackQuery, callback_data: PageCb, repo: Repository, mail: MailService, settings: Settings) -> None:
    """Листание списка писем."""
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer("Загружаю…")
    await _show_page(call.message, acc, callback_data.offset, mail, settings, edit=True)


@router.callback_query(OpenCb.filter())
async def open_message(
    call: CallbackQuery,
    callback_data: OpenCb,
    bot: Bot,
    repo: Repository,
    mail: MailService,
    settings: Settings,
) -> None:
    """Открывает письмо целиком."""
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer("Открываю письмо…")
    try:
        email = await mail.fetch(acc.credentials, callback_data.uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось открыть письмо uid=%s: %s", acc.id, callback_data.uid, exc)
        await call.message.answer(f"❌ Не удалось открыть письмо: <code>{esc(str(exc)[:300])}</code>")
        return
    if email is None:
        await call.message.answer("Письмо не найдено — возможно, его уже удалили.")
        return

    ref_id = await repo.save_message(acc, email.uid, acc.uid_validity, email.reply_meta())
    logger.info("user=%s открыл письмо uid=%s из ящика id=%s", call.from_user.id, email.uid, acc.id)
    await send_email(
        bot,
        call.from_user.id,
        account_title=acc.title,
        email=email,
        ref_id=ref_id,
        title="✉️ Письмо",
        attach_html=settings.attach_html_original,
    )


@router.callback_query(MsgCb.filter(F.action == "seen"))
async def mark_seen(call: CallbackQuery, callback_data: MsgCb, repo: Repository, mail: MailService) -> None:
    """Помечает письмо прочитанным на сервере."""
    found = await repo.get_message(call.from_user.id, callback_data.ref_id)
    if found is None:
        await call.answer("Письмо не найдено", show_alert=True)
        return
    ref, acc = found
    try:
        await mail.mark_seen(acc.credentials, ref.uid)
    except Exception as exc:  # noqa: BLE001
        await call.answer(f"Ошибка: {str(exc)[:150]}", show_alert=True)
        return
    await call.answer("Отмечено как прочитанное ✅")
