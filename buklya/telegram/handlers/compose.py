"""
Написание писем: ответ на письмо и новое письмо.

Письмо собирается в «черновик» из любого количества сообщений:
текст с форматированием Telegram (жирный, курсив, ссылки, код, цитаты),
фото, документы, видео, аудио, голосовые. Под черновиком всегда висит
панель с кнопками «Отправить», «Предпросмотр», «Тема», «Копия» и т.д.

Черновик хранится в FSM-хранилище (в памяти процесса): в нём только
file_id файлов Telegram, сами файлы скачиваются в момент отправки.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from email.utils import getaddresses, parseaddr

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from buklya.db.repository import AccountInfo, Repository
from buklya.mail.composer import format_address, guess_content_type, parse_recipients, reply_subject, telegram_html_to_email_html
from buklya.mail.models import Attachment
from buklya.services.mail_service import MailService
from buklya.telegram import keyboards as kb
from buklya.telegram.callbacks import AccountCb, ComposeCb, MsgCb
from buklya.telegram.render import esc
from buklya.telegram.states import Compose

logger = logging.getLogger(__name__)
router = Router(name="compose")

#: Лимит Bot API на скачивание файла ботом
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024

#: Блокировки по пользователям: части альбома приходят почти одновременно,
#: без блокировки обновления черновика могут потерять друг друга.
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ============================================================================ helpers
def _empty_draft(account: AccountInfo, **extra) -> dict:
    """Начальные данные черновика."""
    draft = {
        "account_id": account.id,
        "account_title": account.title,
        "to": [],
        "cc": [],
        "subject": "",
        "in_reply_to": "",
        "references": "",
        "html_parts": [],
        "text_parts": [],
        "files": [],
        "panel_id": None,
    }
    draft.update(extra)
    return draft


def _panel_text(data: dict) -> str:
    """Текст панели черновика."""
    text_len = sum(len(t) for t in data.get("text_parts", []))
    parts = len(data.get("html_parts", []))
    files = data.get("files", [])
    lines = [
        "📝 <b>Черновик письма</b>",
        f"<b>От:</b> {esc(data.get('account_title', ''))}",
        f"<b>Кому:</b> {esc(', '.join(data.get('to', [])) or '—')}",
    ]
    if data.get("cc"):
        lines.append(f"<b>Копия:</b> {esc(', '.join(data['cc']))}")
    lines.append(f"<b>Тема:</b> {esc(data.get('subject') or '(без темы)')}")
    lines.append(f"<b>Текст:</b> {parts} сообщ., {text_len} симв." if parts else "<b>Текст:</b> пока пусто")
    if files:
        names = ", ".join(esc(f["name"]) for f in files)
        lines.append(f"<b>Вложения ({len(files)}):</b> {names}")
    lines.append("\n<i>Пишите текст и присылайте файлы — можно несколькими сообщениями. Затем «📤 Отправить».</i>")
    return "\n".join(lines)


async def _refresh_panel(message: Message, state: FSMContext) -> None:
    """Удаляет старую панель и показывает новую внизу чата."""
    data = await state.get_data()
    old_id = data.get("panel_id")
    if old_id:
        try:
            await message.bot.delete_message(message.chat.id, old_id)
        except TelegramAPIError:
            pass
    panel = await message.bot.send_message(message.chat.id, _panel_text(data), reply_markup=kb.compose_panel())
    await state.update_data(panel_id=panel.message_id)


def _extract_file(message: Message) -> dict | None:
    """Описание файла из сообщения (file_id, имя, тип, размер) или None."""
    uid = message.message_id
    if message.document:
        d = message.document
        name = d.file_name or f"file_{uid}"
        return {"file_id": d.file_id, "name": name, "mime": d.mime_type or guess_content_type(name), "size": d.file_size or 0}
    if message.photo:
        p = message.photo[-1]  # самое большое разрешение
        return {"file_id": p.file_id, "name": f"photo_{uid}.jpg", "mime": "image/jpeg", "size": p.file_size or 0}
    if message.video:
        v = message.video
        name = v.file_name or f"video_{uid}.mp4"
        return {"file_id": v.file_id, "name": name, "mime": v.mime_type or "video/mp4", "size": v.file_size or 0}
    if message.audio:
        a = message.audio
        name = a.file_name or f"audio_{uid}.mp3"
        return {"file_id": a.file_id, "name": name, "mime": a.mime_type or "audio/mpeg", "size": a.file_size or 0}
    if message.voice:
        v = message.voice
        return {"file_id": v.file_id, "name": f"voice_{uid}.ogg", "mime": v.mime_type or "audio/ogg", "size": v.file_size or 0}
    if message.video_note:
        v = message.video_note
        return {"file_id": v.file_id, "name": f"video_note_{uid}.mp4", "mime": "video/mp4", "size": v.file_size or 0}
    if message.animation:
        a = message.animation
        name = a.file_name or f"animation_{uid}.mp4"
        return {"file_id": a.file_id, "name": name, "mime": a.mime_type or "video/mp4", "size": a.file_size or 0}
    if message.sticker and not message.sticker.is_animated and not message.sticker.is_video:
        s = message.sticker
        return {"file_id": s.file_id, "name": f"sticker_{uid}.webp", "mime": "image/webp", "size": s.file_size or 0}
    return None


async def _start_body(message: Message, state: FSMContext, intro: str) -> None:
    """Переводит диалог в режим набора текста и показывает панель."""
    await state.set_state(Compose.body)
    await message.answer(intro + "\n\n✍️ Пишите текст письма. Форматирование и вложения сохранятся.")
    await _refresh_panel(message, state)


# ============================================================================ ответ на письмо
@router.callback_query(MsgCb.filter(F.action.in_({"reply", "replyall"})))
async def reply_start(call: CallbackQuery, callback_data: MsgCb, state: FSMContext, repo: Repository) -> None:
    """Начинает ответ на письмо."""
    found = await repo.get_message(call.from_user.id, callback_data.ref_id)
    if found is None:
        await call.answer("Письмо не найдено", show_alert=True)
        return
    ref, acc = found
    await call.answer()
    meta = ref.meta
    own = acc.credentials.email.lower()

    # Кому отвечаем: Reply-To, если есть, иначе отправитель
    to = [
        format_address(name, addr)
        for name, addr in getaddresses([meta.get("reply_to") or meta.get("from") or ""])
        if addr
    ]
    cc: list[str] = []
    if callback_data.action == "replyall":
        seen = {parseaddr(t)[1].lower() for t in to} | {own}
        for name, addr in getaddresses([meta.get("to", ""), meta.get("cc", "")]):
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                cc.append(format_address(name, addr))

    await state.clear()
    await state.set_data(
        _empty_draft(
            acc,
            to=to,
            cc=cc,
            subject=reply_subject(meta.get("subject", "")),
            in_reply_to=meta.get("message_id", ""),
            references=meta.get("references", ""),
        )
    )
    logger.info("user=%s начал ответ на письмо ref=%s (%s)", call.from_user.id, ref.id, callback_data.action)
    await _start_body(call.message, state, f"↩️ Ответ на письмо «{esc(meta.get('subject', ''))}»")


# ============================================================================ новое письмо
@router.message(Command("compose"))
@router.message(F.text == kb.BTN_COMPOSE)
async def compose_start(message: Message, state: FSMContext, repo: Repository) -> None:
    """Новое письмо: выбор ящика-отправителя."""
    await state.clear()
    accounts = await repo.list_accounts(message.chat.id)
    if not accounts:
        await message.answer("Сначала подключите ящик: /add")
        return
    if len(accounts) == 1:
        await _ask_recipients(message, state, accounts[0])
        return
    await message.answer("С какого ящика отправить письмо?", reply_markup=kb.choose_account(accounts, "compose"))


@router.callback_query(AccountCb.filter(F.action == "compose"))
async def compose_account(call: CallbackQuery, callback_data: AccountCb, state: FSMContext, repo: Repository) -> None:
    acc = await repo.get_account(call.from_user.id, callback_data.account_id)
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await _ask_recipients(call.message, state, acc)


async def _ask_recipients(message: Message, state: FSMContext, acc: AccountInfo) -> None:
    await state.set_data(_empty_draft(acc))
    await state.set_state(Compose.to)
    await message.answer(
        f"✉️ Новое письмо с <b>{esc(acc.title)}</b>\n\n"
        "Кому? Один или несколько адресов через запятую:\n<code>ivanov@mail.ru, Пётр &lt;petr@ya.ru&gt;</code>",
        reply_markup=kb.compose_cancel(),
    )


@router.message(Compose.to, F.text)
async def compose_to(message: Message, state: FSMContext) -> None:
    try:
        to = parse_recipients(message.text)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(str(exc))}. Попробуйте ещё раз:", reply_markup=kb.compose_cancel())
        return
    await state.update_data(to=to)
    await state.set_state(Compose.subject)
    await message.answer("Тема письма? (отправьте «-», чтобы оставить без темы)", reply_markup=kb.compose_cancel())


@router.message(Compose.subject, F.text)
async def compose_subject(message: Message, state: FSMContext) -> None:
    subject = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(subject=subject)
    await _start_body(message, state, "✉️ Новое письмо")


# ============================================================================ набор текста
@router.message(Compose.body)
async def compose_body(message: Message, state: FSMContext) -> None:
    """Добавляет очередное сообщение (текст и/или файл) в черновик."""
    async with _locks[message.chat.id]:
        data = await state.get_data()
        html_parts: list[str] = list(data.get("html_parts", []))
        text_parts: list[str] = list(data.get("text_parts", []))
        files: list[dict] = list(data.get("files", []))
        warning = ""

        # Текст или подпись к медиа — с форматированием (html_text учитывает entities)
        if message.text or message.caption:
            html_parts.append(message.html_text)
            text_parts.append(message.text or message.caption or "")

        file_info = _extract_file(message)
        if file_info:
            if file_info["size"] > TG_DOWNLOAD_LIMIT:
                warning = (
                    f"⚠️ Файл <b>{esc(file_info['name'])}</b> больше 20 МБ — Telegram не даёт ботам "
                    "скачивать такие файлы, он не будет приложен."
                )
            else:
                files.append(file_info)
        elif not (message.text or message.caption):
            warning = "⚠️ Этот тип сообщения нельзя добавить в письмо."

        await state.update_data(html_parts=html_parts, text_parts=text_parts, files=files)
        if warning:
            await message.answer(warning)
        await _refresh_panel(message, state)


@router.message(Compose.edit_subject, F.text)
async def edit_subject(message: Message, state: FSMContext) -> None:
    subject = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(subject=subject)
    await state.set_state(Compose.body)
    await _refresh_panel(message, state)


@router.message(Compose.edit_cc, F.text)
async def edit_cc(message: Message, state: FSMContext) -> None:
    if message.text.strip() == "-":
        cc: list[str] = []
    else:
        try:
            cc = parse_recipients(message.text)
        except ValueError as exc:
            await message.answer(f"⚠️ {esc(str(exc))}. Попробуйте ещё раз или «-»:")
            return
    await state.update_data(cc=cc)
    await state.set_state(Compose.body)
    await _refresh_panel(message, state)


# ============================================================================ кнопки черновика
@router.callback_query(ComposeCb.filter())
async def compose_buttons(
    call: CallbackQuery,
    callback_data: ComposeCb,
    state: FSMContext,
    bot: Bot,
    repo: Repository,
    mail: MailService,
) -> None:
    current = await state.get_state()
    if current is None or not current.startswith(Compose.__name__):
        await call.answer("Черновик уже закрыт", show_alert=True)
        return
    action = callback_data.action

    if action == "cancel":
        await state.clear()
        await call.answer("Отменено")
        await call.message.edit_text("❌ Письмо отменено.")
        return

    if current != Compose.body.state and action != "cancel":
        await call.answer()
        return

    if action == "clear":
        await state.update_data(html_parts=[], text_parts=[], files=[])
        await call.answer("Текст и вложения удалены")
        await _refresh_panel(call.message, state)
    elif action == "subject":
        await call.answer()
        await state.set_state(Compose.edit_subject)
        await call.message.answer("Новая тема письма («-» — без темы):")
    elif action == "cc":
        await call.answer()
        await state.set_state(Compose.edit_cc)
        await call.message.answer("Копия (адреса через запятую, «-» — убрать копию):")
    elif action == "preview":
        await call.answer()
        await _preview(call.message, state)
    elif action == "send":
        await _send(call, state, bot, repo, mail)


async def _preview(message: Message, state: FSMContext) -> None:
    """Показывает, как будет выглядеть письмо."""
    data = await state.get_data()
    body = "\n\n".join(data.get("html_parts", [])) or "<i>(текст пуст)</i>"
    files = data.get("files", [])
    head = (
        f"👀 <b>Предпросмотр</b>\n<b>Кому:</b> {esc(', '.join(data.get('to', [])))}\n"
        f"<b>Тема:</b> {esc(data.get('subject') or '(без темы)')}\n━━━━━━━━━━━━\n"
    )
    tail = ""
    if files:
        tail = "\n━━━━━━━━━━━━\n📎 " + ", ".join(esc(f["name"]) for f in files)
    text = head + body + tail
    if len(text) > 4096:
        text = head + "<i>(текст слишком длинный для предпросмотра, но будет отправлен целиком)</i>" + tail
    try:
        await message.answer(text, disable_web_page_preview=True)
    except TelegramAPIError:
        await message.answer(head + "<i>(не удалось отобразить текст)</i>" + tail)
    await _refresh_panel(message, state)


async def _send(call: CallbackQuery, state: FSMContext, bot: Bot, repo: Repository, mail: MailService) -> None:
    """Скачивает вложения из Telegram, собирает и отправляет письмо."""
    data = await state.get_data()
    if not data.get("html_parts") and not data.get("files"):
        await call.answer("Письмо пустое — напишите текст или приложите файл", show_alert=True)
        return
    if not data.get("to"):
        await call.answer("Не указан получатель", show_alert=True)
        return
    acc = await repo.get_account(call.from_user.id, data["account_id"])
    if acc is None:
        await call.answer("Ящик не найден", show_alert=True)
        return

    await call.answer("Отправляю…")
    await call.message.edit_text("⏳ Отправляю письмо…")

    try:
        attachments = []
        for f in data.get("files", []):
            buffer = await bot.download(f["file_id"])
            attachments.append(Attachment(filename=f["name"], content=buffer.read(), content_type=f["mime"]))

        await mail.send(
            acc.credentials,
            to=data["to"],
            cc=data.get("cc") or None,
            subject=data.get("subject", ""),
            text="\n\n".join(data.get("text_parts", [])),
            html_body=telegram_html_to_email_html("\n\n".join(data.get("html_parts", []))),
            attachments=attachments,
            in_reply_to=data.get("in_reply_to", ""),
            references=data.get("references", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("user=%s: ошибка отправки письма с ящика id=%s: %s", call.from_user.id, acc.id, exc)
        await call.message.edit_text(
            f"❌ Не удалось отправить: <code>{esc(str(exc)[:300])}</code>\nЧерновик сохранён — можно попробовать ещё раз.",
            reply_markup=kb.compose_panel(),
        )
        return

    await state.clear()
    logger.info(
        "user=%s отправил письмо с ящика id=%s (получателей: %d, вложений: %d)",
        call.from_user.id, acc.id, len(data["to"]) + len(data.get("cc", [])), len(data.get("files", [])),
    )
    await call.message.edit_text(
        f"✅ Письмо отправлено с <b>{esc(acc.title)}</b>\n"
        f"<b>Кому:</b> {esc(', '.join(data['to']))}\n<b>Тема:</b> {esc(data.get('subject') or '(без темы)')}"
    )
