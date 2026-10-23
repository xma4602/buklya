"""
Подключение и управление почтовыми ящиками (ВКонтакте).

Логика та же, что в `buklya.telegram.handlers.accounts`; общие части — в
`buklya.services.wizard`.
"""

from __future__ import annotations

import logging

from buklya.db.repository import AccountInfo, DuplicateAccountError
from buklya.i18n import Safe
from buklya.services import wizard
from buklya.vk import keyboards as kb
from buklya.vk import states as st
from buklya.vk.api import VkGateway
from buklya.vk.context import Ctx
from buklya.vk.markup import html_to_vk
from buklya.vk.router import router
from buklya.vk.texts import t

logger = logging.getLogger(__name__)


# ============================================================================ helpers
async def _delete_password(ctx: Ctx) -> None:
    """
    Пытается удалить сообщение с паролем. ВКонтакте разрешает сообществу удалять
    чужие сообщения не везде — если не вышло, просим пользователя удалить его самому.
    """
    if ctx.message and not await ctx.api.delete(ctx.peer_id, ctx.message.cmid):
        await ctx.send(t("accounts.password_not_deleted"))


async def _try_connect(ctx: Ctx) -> None:
    """Проверяет подключение с данными мастера и, если всё хорошо, сохраняет ящик."""
    data = ctx.state.data
    creds = wizard.creds_from_state(data)
    status = await ctx.send(t("accounts.checking", host=creds.imap_host))
    try:
        uid_validity, last_uid = await ctx.mail.check_credentials(creds)
    except Exception as exc:  # noqa: BLE001 — любые сетевые/протокольные ошибки
        logger.info("VK user=%s: не удалось подключить ящик (%s): %s", ctx.user_id, creds.imap_host, exc)
        await ctx.edit(
            status,
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
            keyboard=kb.add_failed(),
        )
        return

    try:
        account = await ctx.repo.add_account(
            ctx.user_id, creds, data.get("provider", "custom"), last_uid=last_uid, uid_validity=uid_validity
        )
    except DuplicateAccountError:
        ctx.state.clear()
        await ctx.edit(status, t("accounts.duplicate", email=creds.email))
        return

    ctx.state.clear()
    await ctx.edit(status, t("accounts.added", title=account.title))
    await ctx.send(t("common.done"), keyboard=kb.main_menu())


def account_card(acc: AccountInfo) -> Safe:
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
    return Safe("\n".join(lines))


async def _own_account(ctx: Ctx) -> AccountInfo | None:
    """Ящик из payload кнопки (с проверкой владельца)."""
    acc = await ctx.repo.get_account(ctx.user_id, int(ctx.payload.get("id") or 0))
    if acc is None:
        await ctx.answer(t.plain("common.account_not_found"))
    return acc


# ============================================================================ подключение ящика
@router.command("add")
async def add_start(ctx: Ctx) -> None:
    """Шаг 1: спрашиваем адрес."""
    ctx.state.clear()
    ctx.state.set(st.ADD_EMAIL)
    await ctx.send(t("accounts.ask_email"), keyboard=kb.add_cancel())


@router.button("acc", "add")
async def add_start_button(ctx: Ctx) -> None:
    await ctx.answer()
    await add_start(ctx)


@router.state(st.ADD_EMAIL)
async def add_email(ctx: Ctx) -> None:
    """Шаг 2: определяем серверы по адресу и спрашиваем пароль."""
    email = ctx.text.lower()
    if not wizard.is_email(email):
        await ctx.send(t("accounts.bad_email"), keyboard=kb.add_cancel())
        return

    preset, source = await ctx.deps.providers.resolve(email)
    ctx.state.update(**wizard.preset_state(email, preset))
    ctx.state.set(st.ADD_PASSWORD)

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
    await ctx.send(text, keyboard=kb.add_cancel())


@router.state(st.ADD_PASSWORD)
async def add_password(ctx: Ctx) -> None:
    """Шаг 3: получаем пароль, удаляем сообщение, проверяем подключение."""
    if not ctx.text:
        return
    ctx.state.update(password=ctx.text)
    await _delete_password(ctx)
    await _try_connect(ctx)


@router.state(st.ADD_LOGIN)
async def add_login(ctx: Ctx) -> None:
    """Логин, отличающийся от адреса (например, без @домена)."""
    if not ctx.text:
        return
    ctx.state.update(login=ctx.text)
    if ctx.state.data.get("password"):
        await _try_connect(ctx)
    else:
        ctx.state.set(st.ADD_PASSWORD)
        await ctx.send(t("accounts.ask_password"), keyboard=kb.add_cancel())


@router.state(st.ADD_IMAP)
async def add_imap(ctx: Ctx) -> None:
    """Ручной ввод IMAP-сервера."""
    try:
        host, port, security = wizard.parse_server(ctx.text, 993)
    except ValueError as exc:
        await ctx.send(t("accounts.input_warning", error=str(exc)), keyboard=kb.add_cancel())
        return
    ctx.state.update(**wizard.imap_state(host, port, security))
    ctx.state.set(st.ADD_SMTP)
    await ctx.send(t("accounts.ask_smtp"), keyboard=kb.add_cancel())


@router.state(st.ADD_SMTP)
async def add_smtp(ctx: Ctx) -> None:
    """Ручной ввод SMTP-сервера и повторная проверка."""
    try:
        host, port, security = wizard.parse_server(ctx.text, 465)
    except ValueError as exc:
        await ctx.send(t("accounts.input_warning", error=str(exc)), keyboard=kb.add_cancel())
        return
    ctx.state.update(**wizard.smtp_state(host, port, security))
    if ctx.state.data.get("password"):
        await _try_connect(ctx)
    else:
        ctx.state.set(st.ADD_PASSWORD)
        await ctx.send(t("accounts.ask_password_short"), keyboard=kb.add_cancel())


@router.button("add")
async def add_buttons(ctx: Ctx) -> None:
    """Кнопки мастера: отмена и варианты после неудачной попытки."""
    if not ctx.state.in_group("add:"):
        await ctx.answer(t.plain("accounts.wizard_finished"))
        return
    await ctx.answer()
    op = ctx.payload.get("op")
    if op == "cancel":
        ctx.state.clear()
        await ctx.edit_current(t("accounts.add_cancelled"))
    elif op == "password":
        ctx.state.set(st.ADD_PASSWORD)
        await ctx.send(t("accounts.ask_password_again"), keyboard=kb.add_cancel())
    elif op == "login":
        ctx.state.set(st.ADD_LOGIN)
        await ctx.send(t("accounts.ask_login"), keyboard=kb.add_cancel())
    elif op == "manual":
        ctx.state.set(st.ADD_IMAP)
        await ctx.send(t("accounts.ask_imap"), keyboard=kb.add_cancel())


# ============================================================================ список и меню ящиков
@router.command("accounts")
async def accounts_list(ctx: Ctx) -> None:
    """Список подключённых ящиков."""
    ctx.state.clear()
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    if not accounts:
        await ctx.send(t("accounts.list_empty"), keyboard=kb.accounts_list([]))
        return
    await ctx.send(t("accounts.list_legend"), keyboard=kb.accounts_list(accounts))


@router.button("acc", "list")
async def accounts_list_button(ctx: Ctx) -> None:
    await ctx.answer()
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    await ctx.edit_current(t("accounts.list_title"), keyboard=kb.accounts_list(accounts))


@router.button("acc", "menu")
async def account_menu(ctx: Ctx) -> None:
    """Карточка ящика с действиями."""
    acc = await _own_account(ctx)
    if acc is None:
        return
    await ctx.answer()
    await ctx.edit_current(account_card(acc), keyboard=kb.account_menu(acc))


@router.button("acc", "toggle")
async def account_toggle(ctx: Ctx) -> None:
    """Включение/выключение уведомлений."""
    acc = await _own_account(ctx)
    if acc is None:
        return
    new_value = not (acc.notify and acc.is_active)
    await ctx.repo.set_notify(ctx.user_id, acc.id, new_value)
    await ctx.answer(t.plain("accounts.notify_enabled" if new_value else "accounts.notify_disabled"))
    acc = await ctx.repo.get_account(ctx.user_id, acc.id)
    await ctx.edit_current(account_card(acc), keyboard=kb.account_menu(acc))


@router.button("acc", "delete")
async def account_delete(ctx: Ctx) -> None:
    """Запрос подтверждения удаления."""
    acc = await _own_account(ctx)
    if acc is None:
        return
    await ctx.answer()
    await ctx.edit_current(t("accounts.delete_confirm", title=acc.title), keyboard=kb.confirm_delete(acc.id))


@router.button("acc", "delete_ok")
async def account_delete_ok(ctx: Ctx) -> None:
    """Удаление ящика."""
    deleted = await ctx.repo.delete_account(ctx.user_id, int(ctx.payload.get("id") or 0))
    await ctx.answer(t.plain("accounts.deleted" if deleted else "common.account_not_found"))
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    await ctx.edit_current(t("accounts.list_title"), keyboard=kb.accounts_list(accounts))


# ============================================================================ смена пароля
@router.button("acc", "passwd")
async def passwd_start(ctx: Ctx) -> None:
    await ctx.answer()
    ctx.state.clear()
    ctx.state.set(st.CHANGE_PASSWORD)
    ctx.state.update(account_id=int(ctx.payload.get("id") or 0))
    await ctx.send(t("accounts.new_password"))


@router.state(st.CHANGE_PASSWORD)
async def passwd_set(ctx: Ctx) -> None:
    """Проверяет новый пароль и сохраняет его."""
    if not ctx.text:
        return
    password = ctx.text
    await _delete_password(ctx)
    acc = await ctx.repo.get_account(ctx.user_id, ctx.state.data.get("account_id", 0))
    if acc is None:
        ctx.state.clear()
        await ctx.send(t("accounts.not_found_full"))
        return
    acc.credentials.password = password
    status = await ctx.send(t("accounts.password_checking"))
    try:
        await ctx.mail.check_credentials(acc.credentials)
    except Exception as exc:  # noqa: BLE001
        await ctx.edit(status, t("accounts.password_rejected", error=wizard.humanize_error(exc)))
        return
    await ctx.repo.update_credentials(ctx.user_id, acc.id, acc.credentials)
    ctx.state.clear()
    logger.info("VK user=%s сменил пароль ящика id=%s", ctx.user_id, acc.id)
    await ctx.edit(status, t("accounts.password_updated", title=acc.title))


# ============================================================================ уведомление поллера
async def notify_deactivated(api: VkGateway, peer_id: int, acc: AccountInfo) -> None:
    """Сообщает пользователю, что ящик отключён из-за ошибок."""
    await api.send(peer_id, html_to_vk(t("accounts.deactivated", title=acc.title, error=(acc.last_error or "")[:300])))
