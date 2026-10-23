"""
Подключение и управление почтовыми ящиками.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from buklya.db.repository import AccountInfo, DuplicateAccountError, Repository
from buklya.mail.providers import ProviderRegistry
from buklya.services import wizard
from buklya.services.mail_service import MailService
from buklya.telegram import keyboards as kb
from buklya.telegram.callbacks import AccountCb, AddCb
from buklya.telegram.states import AddAccount, ChangePassword
from buklya.telegram.texts import t

logger = logging.getLogger(__name__)
router = Router(name="accounts")


# ============================================================================ helpers
async def _delete_quietly(message: Message) -> None:
    """Удаляет сообщение (например, с паролем); ошибки игнорирует."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def _try_connect(message: Message, state: FSMContext, repo: Repository, mail: MailService, user_id: int) -> None:
    """Проверяет подключение с данными из FSM и, если всё хорошо, сохраняет ящик."""
    data = await state.get_data()
    creds = wizard.creds_from_state(data)
    status = await message.answer(t("accounts.checking", host=creds.imap_host))
    try:
        uid_validity, last_uid = await mail.check_credentials(creds)
    except Exception as exc:  # noqa: BLE001 — любые сетевые/протокольные ошибки
        logger.info("user=%s: не удалось подключить ящик (%s): %s", user_id, creds.imap_host, exc)
        await status.edit_text(
            t(
                "accounts.connect_failed",
                error=wizard.humanize_error(exc),
                imap_host=creds.imap_host,
                imap_port=creds.imap_port,
                smtp_host=creds.smtp_host,
                smtp_port=creds.smtp_port,
                smtp_security=creds.smtp_security,
                login=creds.login,
            ),
            reply_markup=kb.add_failed(),
        )
        return

    try:
        account = await repo.add_account(
            user_id, creds, data.get("provider", "custom"), last_uid=last_uid, uid_validity=uid_validity
        )
    except DuplicateAccountError:
        await state.clear()
        await status.edit_text(t("accounts.duplicate", email=creds.email))
        return

    await state.clear()
    await status.edit_text(t("accounts.added", title=account.title))
    await message.answer(t("common.done"), reply_markup=kb.main_menu())


def _account_card(acc: AccountInfo) -> str:
    """Описание ящика для меню."""
    c = acc.credentials
    lines = [
        t("accounts.card_title", title=acc.title),
        t("accounts.card_imap", host=c.imap_host, port=c.imap_port),
        t("accounts.card_smtp", host=c.smtp_host, port=c.smtp_port, security=c.smtp_security),
        t("accounts.card_notify_on" if acc.notify and acc.is_active else "accounts.card_notify_off"),
    ]
    if not acc.is_active:
        lines.append(t("accounts.card_suspended"))
    if acc.last_error:
        lines.append(t("accounts.card_last_error", error=acc.last_error[:200]))
    return "\n".join(lines)


# ============================================================================ подключение ящика
@router.message(Command("add"))
@router.message(F.text == kb.BTN_ADD)
async def add_start(message: Message, state: FSMContext) -> None:
    """Шаг 1: спрашиваем адрес."""
    await state.clear()
    await state.set_state(AddAccount.email)
    await message.answer(t("accounts.ask_email"), reply_markup=kb.add_cancel())


@router.callback_query(AccountCb.filter(F.action == "add"))
async def add_start_cb(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await add_start(call.message, state)


@router.message(AddAccount.email, F.text)
async def add_email(message: Message, state: FSMContext, providers: ProviderRegistry) -> None:
    """Шаг 2: определяем серверы по адресу и спрашиваем пароль."""
    email = message.text.strip().lower()
    if not wizard.is_email(email):
        await message.answer(t("accounts.bad_email"), reply_markup=kb.add_cancel())
        return

    preset, source = await providers.resolve(email)
    await state.update_data(**wizard.preset_state(email, preset))
    await state.set_state(AddAccount.password)

    text = t(
        "accounts.provider_found",
        title=preset.title,
        source=t.plain(wizard.SOURCE_KEYS[source]),
        imap=preset.imap_host,
        smtp=preset.smtp_host,
    )
    if preset.hint:
        text += "\n\n" + t("accounts.provider_hint", hint=preset.hint)
    text += "\n\n" + t("accounts.password_hint")
    await message.answer(text, reply_markup=kb.add_cancel(), disable_web_page_preview=True)


@router.message(AddAccount.password, F.text)
async def add_password(message: Message, state: FSMContext, repo: Repository, mail: MailService, user_id: int) -> None:
    """Шаг 3: получаем пароль, удаляем сообщение, проверяем подключение."""
    password = message.text.strip()
    await _delete_quietly(message)
    await state.update_data(password=password)
    await _try_connect(message, state, repo, mail, user_id)


@router.message(AddAccount.login, F.text)
async def add_login(message: Message, state: FSMContext, repo: Repository, mail: MailService, user_id: int) -> None:
    """Логин, отличающийся от адреса (например, без @домена)."""
    await state.update_data(login=message.text.strip())
    data = await state.get_data()
    if data.get("password"):
        await _try_connect(message, state, repo, mail, user_id)
    else:
        await state.set_state(AddAccount.password)
        await message.answer(t("accounts.ask_password"), reply_markup=kb.add_cancel())


@router.message(AddAccount.imap, F.text)
async def add_imap(message: Message, state: FSMContext) -> None:
    """Ручной ввод IMAP-сервера."""
    try:
        host, port, security = wizard.parse_server(message.text, 993)
    except ValueError as exc:
        await message.answer(t("accounts.input_warning", error=str(exc)), reply_markup=kb.add_cancel())
        return
    await state.update_data(**wizard.imap_state(host, port, security))
    await state.set_state(AddAccount.smtp)
    await message.answer(t("accounts.ask_smtp"), reply_markup=kb.add_cancel())


@router.message(AddAccount.smtp, F.text)
async def add_smtp(message: Message, state: FSMContext, repo: Repository, mail: MailService, user_id: int) -> None:
    """Ручной ввод SMTP-сервера и повторная проверка."""
    try:
        host, port, security = wizard.parse_server(message.text, 465)
    except ValueError as exc:
        await message.answer(t("accounts.input_warning", error=str(exc)), reply_markup=kb.add_cancel())
        return
    await state.update_data(**wizard.smtp_state(host, port, security))
    data = await state.get_data()
    if data.get("password"):
        await _try_connect(message, state, repo, mail, user_id)
    else:
        await state.set_state(AddAccount.password)
        await message.answer(t("accounts.ask_password_short"), reply_markup=kb.add_cancel())


@router.callback_query(AddCb.filter(), StateFilter(AddAccount))
async def add_buttons(call: CallbackQuery, callback_data: AddCb, state: FSMContext) -> None:
    """Кнопки мастера после неудачной попытки."""
    await call.answer()
    action = callback_data.action
    if action == "cancel":
        await state.clear()
        await call.message.edit_text(t("accounts.add_cancelled"))
    elif action == "password":
        await state.set_state(AddAccount.password)
        await call.message.answer(t("accounts.ask_password_again"), reply_markup=kb.add_cancel())
    elif action == "login":
        await state.set_state(AddAccount.login)
        await call.message.answer(t("accounts.ask_login"), reply_markup=kb.add_cancel())
    elif action == "manual":
        await state.set_state(AddAccount.imap)
        await call.message.answer(t("accounts.ask_imap"), reply_markup=kb.add_cancel())


@router.callback_query(AddCb.filter())
async def add_buttons_stale(call: CallbackQuery) -> None:
    """Кнопки мастера, нажатые после его завершения."""
    await call.answer(t.plain("accounts.wizard_finished"), show_alert=True)


# ============================================================================ список и меню ящиков
@router.message(Command("accounts"))
@router.message(F.text == kb.BTN_ACCOUNTS)
async def accounts_list(message: Message, state: FSMContext, repo: Repository, user_id: int) -> None:
    """Список подключённых ящиков."""
    await state.clear()
    accounts = await repo.list_accounts(user_id)
    if not accounts:
        await message.answer(t("accounts.list_empty"), reply_markup=kb.accounts_list([]))
        return
    await message.answer(t("accounts.list_legend"), reply_markup=kb.accounts_list(accounts))


@router.callback_query(AccountCb.filter(F.action == "list"))
async def accounts_list_cb(call: CallbackQuery, repo: Repository, user_id: int) -> None:
    await call.answer()
    accounts = await repo.list_accounts(user_id)
    await call.message.edit_text(t("accounts.list_title"), reply_markup=kb.accounts_list(accounts))


@router.callback_query(AccountCb.filter(F.action == "menu"))
async def account_menu(call: CallbackQuery, callback_data: AccountCb, repo: Repository, user_id: int) -> None:
    """Карточка ящика с действиями."""
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(_account_card(acc), reply_markup=kb.account_menu(acc))


@router.callback_query(AccountCb.filter(F.action == "toggle"))
async def account_toggle(call: CallbackQuery, callback_data: AccountCb, repo: Repository, user_id: int) -> None:
    """Включение/выключение уведомлений."""
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    new_value = not (acc.notify and acc.is_active)
    await repo.set_notify(user_id, acc.id, new_value)
    await call.answer(t.plain("accounts.notify_enabled" if new_value else "accounts.notify_disabled"))
    acc = await repo.get_account(user_id, acc.id)
    await call.message.edit_text(_account_card(acc), reply_markup=kb.account_menu(acc))


@router.callback_query(AccountCb.filter(F.action == "delete"))
async def account_delete(call: CallbackQuery, callback_data: AccountCb, repo: Repository, user_id: int) -> None:
    """Запрос подтверждения удаления."""
    acc = await repo.get_account(user_id, callback_data.account_id)
    if acc is None:
        await call.answer(t.plain("common.account_not_found"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(t("accounts.delete_confirm", title=acc.title), reply_markup=kb.confirm_delete(acc.id))


@router.callback_query(AccountCb.filter(F.action == "delete_ok"))
async def account_delete_ok(call: CallbackQuery, callback_data: AccountCb, repo: Repository, user_id: int) -> None:
    """Удаление ящика."""
    deleted = await repo.delete_account(user_id, callback_data.account_id)
    await call.answer(t.plain("accounts.deleted" if deleted else "common.account_not_found"))
    accounts = await repo.list_accounts(user_id)
    await call.message.edit_text(t("accounts.list_title"), reply_markup=kb.accounts_list(accounts))


# ============================================================================ смена пароля
@router.callback_query(AccountCb.filter(F.action == "passwd"))
async def passwd_start(call: CallbackQuery, callback_data: AccountCb, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await state.set_state(ChangePassword.password)
    await state.update_data(account_id=callback_data.account_id)
    await call.message.answer(t("accounts.new_password"))


@router.message(ChangePassword.password, F.text)
async def passwd_set(message: Message, state: FSMContext, repo: Repository, mail: MailService, user_id: int) -> None:
    """Проверяет новый пароль и сохраняет его."""
    password = message.text.strip()
    await _delete_quietly(message)
    data = await state.get_data()
    acc = await repo.get_account(user_id, data.get("account_id", 0))
    if acc is None:
        await state.clear()
        await message.answer(t("accounts.not_found_full"))
        return
    acc.credentials.password = password
    status = await message.answer(t("accounts.password_checking"))
    try:
        await mail.check_credentials(acc.credentials)
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(t("accounts.password_rejected", error=wizard.humanize_error(exc)))
        return
    await repo.update_credentials(user_id, acc.id, acc.credentials)
    await state.clear()
    logger.info("user=%s сменил пароль ящика id=%s", user_id, acc.id)
    await status.edit_text(t("accounts.password_updated", title=acc.title))


# ============================================================================ уведомление поллера
async def notify_deactivated(bot: Bot, chat_id: int, acc: AccountInfo) -> None:
    """Сообщает пользователю, что ящик отключён из-за ошибок."""
    try:
        await bot.send_message(
            chat_id,
            t("accounts.deactivated", title=acc.title, error=(acc.last_error or "")[:300]),
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось уведомить chat=%s: %s", chat_id, exc)
