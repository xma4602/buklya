"""
Подключение и управление почтовыми ящиками.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from buklya.db.repository import AccountInfo, DuplicateAccountError, Repository
from buklya.mail.models import MailCredentials
from buklya.mail.providers import ProviderRegistry
from buklya.services.mail_service import MailService
from buklya.telegram import keyboards as kb
from buklya.telegram.callbacks import AccountCb, AddCb
from buklya.telegram.render import esc
from buklya.telegram.states import AddAccount, ChangePassword

logger = logging.getLogger(__name__)
router = Router(name="accounts")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ============================================================================ helpers
async def _delete_quietly(message: Message) -> None:
    """Удаляет сообщение (например, с паролем); ошибки игнорирует."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


def _creds_from_state(data: dict) -> MailCredentials:
    """Собирает учётные данные из данных FSM мастера подключения."""
    return MailCredentials(
        email=data["email"],
        login=data.get("login") or data["email"],
        password=data.get("password", ""),
        imap_host=data["imap_host"],
        imap_port=int(data.get("imap_port", 993)),
        imap_ssl=bool(data.get("imap_ssl", True)),
        smtp_host=data.get("smtp_host", ""),
        smtp_port=int(data.get("smtp_port", 465)),
        smtp_security=data.get("smtp_security", "ssl"),
        save_to_sent=bool(data.get("save_to_sent", True)),
    )


def _humanize_error(exc: Exception) -> str:
    """Текст ошибки для пользователя (без трассировок)."""
    text = str(exc) or exc.__class__.__name__
    return text[:400]


async def _try_connect(message: Message, state: FSMContext, repo: Repository, mail: MailService) -> None:
    """Проверяет подключение с данными из FSM и, если всё хорошо, сохраняет ящик."""
    data = await state.get_data()
    creds = _creds_from_state(data)
    status = await message.answer(f"⏳ Проверяю подключение к <b>{esc(creds.imap_host)}</b>…")
    try:
        uid_validity, last_uid = await mail.check_credentials(creds)
    except Exception as exc:  # noqa: BLE001 — любые сетевые/протокольные ошибки
        logger.info("user=%s: не удалось подключить ящик (%s): %s", message.chat.id, creds.imap_host, exc)
        await status.edit_text(
            "❌ Не удалось подключиться.\n"
            f"<code>{esc(_humanize_error(exc))}</code>\n\n"
            f"IMAP: <code>{esc(creds.imap_host)}:{creds.imap_port}</code>\n"
            f"SMTP: <code>{esc(creds.smtp_host)}:{creds.smtp_port}</code> ({creds.smtp_security})\n"
            f"Логин: <code>{esc(creds.login)}</code>\n\n"
            "Что попробовать?",
            reply_markup=kb.add_failed(),
        )
        return

    try:
        account = await repo.add_account(
            message.chat.id, creds, data.get("provider", "custom"), last_uid=last_uid, uid_validity=uid_validity
        )
    except DuplicateAccountError:
        await state.clear()
        await status.edit_text(f"ℹ️ Ящик <b>{esc(creds.email)}</b> уже подключён.")
        return

    await state.clear()
    await status.edit_text(
        f"✅ Ящик <b>{esc(account.title)}</b> подключён!\n"
        "Новые письма будут приходить сюда. Последние письма — кнопка «📬 Последние письма»."
    )
    await message.answer("Готово 🦉", reply_markup=kb.main_menu())


def _account_card(acc: AccountInfo) -> str:
    """Описание ящика для меню."""
    c = acc.credentials
    lines = [
        f"📮 <b>{esc(acc.title)}</b>",
        f"IMAP: <code>{esc(c.imap_host)}:{c.imap_port}</code>",
        f"SMTP: <code>{esc(c.smtp_host)}:{c.smtp_port}</code> ({c.smtp_security})",
        f"Уведомления: {'включены 🔔' if acc.notify and acc.is_active else 'выключены 🔕'}",
    ]
    if not acc.is_active:
        lines.append("⚠️ Ящик приостановлен из-за повторяющихся ошибок. Проверьте пароль и включите уведомления.")
    if acc.last_error:
        lines.append(f"Последняя ошибка: <code>{esc(acc.last_error[:200])}</code>")
    return "\n".join(lines)


# ============================================================================ подключение ящика
@router.message(Command("add"))
@router.message(F.text == kb.BTN_ADD)
async def add_start(message: Message, state: FSMContext) -> None:
    """Шаг 1: спрашиваем адрес."""
    await state.clear()
    await state.set_state(AddAccount.email)
    await message.answer(
        "📮 Введите адрес почты, например <code>ivanov@yandex.ru</code>.\n"
        "Поддерживаются Яндекс, Gmail, Mail.ru, ssau.ru и любые ящики с IMAP/SMTP.",
        reply_markup=kb.add_cancel(),
    )


@router.callback_query(AccountCb.filter(F.action == "add"))
async def add_start_cb(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await add_start(call.message, state)


@router.message(AddAccount.email, F.text)
async def add_email(message: Message, state: FSMContext, providers: ProviderRegistry) -> None:
    """Шаг 2: определяем серверы по адресу и спрашиваем пароль."""
    email = message.text.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer("Это не похоже на адрес почты. Попробуйте ещё раз:", reply_markup=kb.add_cancel())
        return

    preset, source = await providers.resolve(email)
    await state.update_data(
        email=email,
        login=email,
        provider=preset.key,
        imap_host=preset.imap_host,
        imap_port=preset.imap_port,
        imap_ssl=True,
        smtp_host=preset.smtp_host,
        smtp_port=preset.smtp_port,
        smtp_security=preset.smtp_security,
        save_to_sent=preset.save_to_sent,
    )
    await state.set_state(AddAccount.password)

    where = {"domain": "по домену", "mx": "по MX-записи домена", "guess": "предположительно"}[source]
    hint = f"\n\n💡 {esc(preset.hint)}" if preset.hint else ""
    await message.answer(
        f"Провайдер: <b>{esc(preset.title)}</b> ({where})\n"
        f"IMAP <code>{esc(preset.imap_host)}</code>, SMTP <code>{esc(preset.smtp_host)}</code>{hint}\n\n"
        "🔑 Теперь отправьте пароль (или пароль приложения).\n"
        "<i>Сообщение с паролем я сразу удалю, а в базе он хранится только зашифрованным.</i>",
        reply_markup=kb.add_cancel(),
        disable_web_page_preview=True,
    )


@router.message(AddAccount.password, F.text)
async def add_password(message: Message, state: FSMContext, repo: Repository, mail: MailService) -> None:
    """Шаг 3: получаем пароль, удаляем сообщение, проверяем подключение."""
    password = message.text.strip()
    await _delete_quietly(message)
    await state.update_data(password=password)
    await _try_connect(message, state, repo, mail)


@router.message(AddAccount.login, F.text)
async def add_login(message: Message, state: FSMContext, repo: Repository, mail: MailService) -> None:
    """Логин, отличающийся от адреса (например, без @домена)."""
    await state.update_data(login=message.text.strip())
    data = await state.get_data()
    if data.get("password"):
        await _try_connect(message, state, repo, mail)
    else:
        await state.set_state(AddAccount.password)
        await message.answer("🔑 Теперь отправьте пароль:", reply_markup=kb.add_cancel())


def _parse_server(text: str, default_port: int) -> tuple[str, int, str | None]:
    """Разбирает строку `host[:port][:ssl|starttls|none]`."""
    parts = [p.strip() for p in text.strip().split(":") if p.strip()]
    if not parts or " " in parts[0]:
        raise ValueError("Укажите сервер в виде host:port")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else default_port
    security = parts[2].lower() if len(parts) > 2 else None
    if security and security not in ("ssl", "starttls", "none"):
        raise ValueError("Тип защиты: ssl, starttls или none")
    return host, port, security


@router.message(AddAccount.imap, F.text)
async def add_imap(message: Message, state: FSMContext) -> None:
    """Ручной ввод IMAP-сервера."""
    try:
        host, port, security = _parse_server(message.text, 993)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(str(exc))}", reply_markup=kb.add_cancel())
        return
    await state.update_data(imap_host=host, imap_port=port, imap_ssl=security != "none" and port != 143)
    await state.set_state(AddAccount.smtp)
    await message.answer(
        "Теперь SMTP-сервер: <code>host:port</code> или <code>host:port:starttls</code>\n"
        "Например: <code>smtp.example.ru:465</code> или <code>smtp.example.ru:587:starttls</code>",
        reply_markup=kb.add_cancel(),
    )


@router.message(AddAccount.smtp, F.text)
async def add_smtp(message: Message, state: FSMContext, repo: Repository, mail: MailService) -> None:
    """Ручной ввод SMTP-сервера и повторная проверка."""
    try:
        host, port, security = _parse_server(message.text, 465)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(str(exc))}", reply_markup=kb.add_cancel())
        return
    security = security or ("starttls" if port == 587 else "ssl" if port == 465 else "none")
    await state.update_data(smtp_host=host, smtp_port=port, smtp_security=security, provider="custom")
    data = await state.get_data()
    if data.get("password"):
        await _try_connect(message, state, repo, mail)
    else:
        await state.set_state(AddAccount.password)
        await message.answer("🔑 Отправьте пароль:", reply_markup=kb.add_cancel())


@router.callback_query(AddCb.filter(), StateFilter(AddAccount))
async def add_buttons(call: CallbackQuery, callback_data: AddCb, state: FSMContext) -> None:
    """Кнопки мастера после неудачной попытки."""
    await call.answer()
    action = callback_data.action
    if action == "cancel":
        await state.clear()
        await call.message.edit_text("Подключение ящика отменено.")
    elif action == "password":
        await state.set_state(AddAccount.password)
        await call.message.answer("🔑 Отправьте пароль ещё раз:", reply_markup=kb.add_cancel())
    elif action == "login":
        await state.set_state(AddAccount.login)
        await call.message.answer(
            "👤 Отправьте логин для входа (например, только часть до @):", reply_markup=kb.add_cancel()
        )
    elif action == "manual":
        await state.set_state(AddAccount.imap)
        await call.message.answer(
            "⚙️ Введите IMAP-сервер: <code>host:port</code>\nНапример: <code>imap.example.ru:993</code>",
            reply_markup=kb.add_cancel(),
        )


@router.callback_query(AddCb.filter())
async def add_buttons_stale(call: CallbackQuery) -> None:
    """Кнопки мастера, нажатые после его завершения."""
    await call.answer("Этот диалог уже завершён. Начните заново: /add", show_alert=True)


# ============================================================================ список и меню ящиков
@router.message(Command("accounts"))
@router.message(F.text == kb.BTN_ACCOUNTS)
async def accounts_list(message: Message, state: FSMContext, repo: Repository) -> None:
    """Список подключённых ящиков."""
    await state.clear()
    accounts = await repo.list_accounts(message.chat.id)
    if not accounts:
        await message.answer("У вас пока нет подключённых ящиков.", reply_markup=kb.accounts_list([]))
        return
    await message.answer("📮 <b>Ваши ящики</b>\n🔔 — уведомления включены, 🔕 — выключены, ⚠️ — ошибка",
                         reply_markup=kb.accounts_list(accounts))


@router.callback_query(AccountCb.filter(F.action == "list"))
async def accounts_list_cb(call: CallbackQuery, repo: Repository) -> None:
    await call.answer()
    accounts = await repo.list_accounts(call.from_user.id)
    await call.message.edit_text("📮 <b>Ваши ящики</b>", reply_markup=kb.accounts_list(accounts))


@router.callback_query(AccountCb.filter(F.action == "menu"))
async def account_menu(call: CallbackQuery, callback_data: AccountCb, repo: Repository) -> None:
    """Карточка ящика с действиями."""
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(_account_card(acc), reply_markup=kb.account_menu(acc))


@router.callback_query(AccountCb.filter(F.action == "toggle"))
async def account_toggle(call: CallbackQuery, callback_data: AccountCb, repo: Repository) -> None:
    """Включение/выключение уведомлений."""
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    new_value = not (acc.notify and acc.is_active)
    await repo.set_notify(call.from_user.id, acc.id, new_value)
    await call.answer("Уведомления включены 🔔" if new_value else "Уведомления выключены 🔕")
    acc = await repo.get_account(call.from_user.id, acc.id)
    await call.message.edit_text(_account_card(acc), reply_markup=kb.account_menu(acc))


@router.callback_query(AccountCb.filter(F.action == "delete"))
async def account_delete(call: CallbackQuery, callback_data: AccountCb, repo: Repository) -> None:
    """Запрос подтверждения удаления."""
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        f"Удалить ящик <b>{esc(acc.title)}</b>?\nЛогин и пароль будут стёрты из базы бота.",
        reply_markup=kb.confirm_delete(acc.id),
    )


@router.callback_query(AccountCb.filter(F.action == "delete_ok"))
async def account_delete_ok(call: CallbackQuery, callback_data: AccountCb, repo: Repository) -> None:
    """Удаление ящика."""
    deleted = await repo.delete_account(call.from_user.id, callback_data.account_id)
    await call.answer("Ящик удалён" if deleted else "Ящик не найден")
    accounts = await repo.list_accounts(call.from_user.id)
    await call.message.edit_text("📮 <b>Ваши ящики</b>", reply_markup=kb.accounts_list(accounts))


# ============================================================================ смена пароля
@router.callback_query(AccountCb.filter(F.action == "passwd"))
async def passwd_start(call: CallbackQuery, callback_data: AccountCb, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await state.set_state(ChangePassword.password)
    await state.update_data(account_id=callback_data.account_id)
    await call.message.answer("🔑 Отправьте новый пароль (сообщение будет удалено). /cancel — отмена.")


@router.message(ChangePassword.password, F.text)
async def passwd_set(message: Message, state: FSMContext, repo: Repository, mail: MailService) -> None:
    """Проверяет новый пароль и сохраняет его."""
    password = message.text.strip()
    await _delete_quietly(message)
    data = await state.get_data()
    acc = await repo.get_account(message.chat.id, data.get("account_id", 0))
    if acc is None:
        await state.clear()
        await message.answer("Ящик не найден.")
        return
    acc.credentials.password = password
    status = await message.answer("⏳ Проверяю…")
    try:
        await mail.check_credentials(acc.credentials)
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(f"❌ Не подошёл: <code>{esc(_humanize_error(exc))}</code>\nПопробуйте ещё раз или /cancel.")
        return
    await repo.update_credentials(message.chat.id, acc.id, acc.credentials)
    await state.clear()
    logger.info("user=%s сменил пароль ящика id=%s", message.chat.id, acc.id)
    await status.edit_text(f"✅ Пароль для <b>{esc(acc.title)}</b> обновлён, уведомления включены.")


# ============================================================================ уведомление поллера
async def notify_deactivated(bot: Bot, acc: AccountInfo) -> None:
    """Сообщает пользователю, что ящик отключён из-за ошибок."""
    try:
        await bot.send_message(
            acc.user_id,
            f"⚠️ Не удаётся проверить ящик <b>{esc(acc.title)}</b> — проверка приостановлена.\n"
            f"Ошибка: <code>{esc((acc.last_error or '')[:300])}</code>\n"
            "Откройте «📮 Мои ящики», смените пароль или включите уведомления снова.",
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось уведомить user=%s: %s", acc.user_id, exc)
