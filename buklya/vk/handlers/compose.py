"""
Написание писем во ВКонтакте: ответ на письмо и новое письмо.

Письмо собирается в черновик из любого количества сообщений: текст с
форматированием ВКонтакте (жирный, курсив, подчёркнутый, ссылки), фото,
документы, голосовые сообщения. Под черновиком — панель «Отправить»,
«Предпросмотр», «Тема», «Копия» и т.д.

В отличие от Telegram, ссылки на файлы ВКонтакте живут ограниченное время,
поэтому файлы скачиваются сразу и хранятся в черновике (в памяти процесса).
"""

from __future__ import annotations

import logging
from typing import Any

from buklya.db.repository import AccountInfo
from buklya.mail.composer import chat_html_to_email_html, guess_content_type, parse_recipients
from buklya.mail.models import Attachment
from buklya.services import drafts
from buklya.vk import keyboards as kb
from buklya.vk import states as st
from buklya.vk.api import VkApiError
from buklya.vk.context import Ctx
from buklya.vk.markup import VK_TEXT_LIMIT, html_to_vk, vk_to_html
from buklya.vk.router import router
from buklya.vk.texts import t

logger = logging.getLogger(__name__)


# ============================================================================ вложения ВКонтакте
def _largest_photo(photo: dict[str, Any]) -> str | None:
    """URL самой большой версии фото."""
    orig = photo.get("orig_photo") or {}
    if orig.get("url"):
        return orig["url"]
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
    return best.get("url")


def describe_attachment(att: dict[str, Any], index: int) -> dict[str, Any] | None:
    """
    Вложение сообщения ВКонтакте -> {"url", "name", "mime", "size"} или None,
    если файл нельзя скачать. Для видео возвращается {"video": True}.
    """
    kind = att.get("type")
    data = att.get(kind) or {}
    if kind == "doc":
        title = data.get("title") or f"file_{index}"
        ext = data.get("ext") or ""
        name = title if not ext or title.lower().endswith("." + ext.lower()) else f"{title}.{ext}"
        return {"url": data.get("url"), "name": name, "mime": guess_content_type(name), "size": data.get("size") or 0}
    if kind == "photo":
        return {"url": _largest_photo(data), "name": f"photo_{data.get('id', index)}.jpg", "mime": "image/jpeg", "size": 0}
    if kind == "audio_message":
        if data.get("link_ogg"):
            return {"url": data["link_ogg"], "name": f"voice_{data.get('id', index)}.ogg", "mime": "audio/ogg", "size": 0}
        return {"url": data.get("link_mp3"), "name": f"voice_{data.get('id', index)}.mp3", "mime": "audio/mpeg", "size": 0}
    if kind == "graffiti":
        return {"url": data.get("url"), "name": f"graffiti_{data.get('id', index)}.png", "mime": "image/png", "size": 0}
    if kind == "audio" and data.get("url"):
        name = f"{data.get('artist', '')} - {data.get('title', '')}".strip(" -") or f"audio_{index}"
        return {"url": data["url"], "name": f"{name}.mp3", "mime": "audio/mpeg", "size": 0}
    if kind == "video":
        return {"video": True}
    return None


# ============================================================================ helpers
async def refresh_panel(ctx: Ctx) -> None:
    """Удаляет старую панель черновика и показывает новую внизу чата."""
    old = ctx.state.data.get("panel_id")
    if old:
        await ctx.api.delete(ctx.peer_id, old)
    panel = await ctx.send(drafts.panel_text(t, ctx.state.data), keyboard=kb.compose_panel())
    ctx.state.update(panel_id=panel)


async def start_body(ctx: Ctx, intro: str) -> None:
    """Переводит диалог в режим набора текста и показывает панель."""
    ctx.state.set(st.COMPOSE_BODY)
    await ctx.send(intro + "\n\n" + t("compose.body_hint"))
    await refresh_panel(ctx)


# ============================================================================ ответ на письмо
@router.button("msg", "reply", "replyall")
async def reply_start(ctx: Ctx) -> None:
    """Начинает ответ на письмо."""
    found = await ctx.repo.get_message(ctx.user_id, int(ctx.payload.get("r") or 0))
    if found is None:
        await ctx.answer(t.plain("common.message_not_found"))
        return
    ref, acc = found
    await ctx.answer()
    ctx.state.clear()
    ctx.state.data = drafts.reply_draft(ref, acc, reply_all=ctx.payload.get("op") == "replyall")
    logger.info("VK user=%s начал ответ на письмо ref=%s (%s)", ctx.user_id, ref.id, ctx.payload.get("op"))
    await start_body(ctx, t("compose.reply_to", subject=ref.meta.get("subject", "")))


# ============================================================================ новое письмо
@router.command("compose")
async def compose_start(ctx: Ctx) -> None:
    """Новое письмо: выбор ящика-отправителя."""
    ctx.state.clear()
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    if not accounts:
        await ctx.send(t("common.need_account"))
        return
    if len(accounts) == 1:
        await _ask_recipients(ctx, accounts[0])
        return
    await ctx.send(t("compose.choose_account"), keyboard=kb.choose_account(accounts, "compose"))


@router.button("acc", "compose")
async def compose_account(ctx: Ctx) -> None:
    acc = await ctx.repo.get_account(ctx.user_id, int(ctx.payload.get("id") or 0))
    if acc is None:
        await ctx.answer(t.plain("common.account_not_found"))
        return
    await ctx.answer()
    ctx.state.clear()
    await _ask_recipients(ctx, acc)


async def _ask_recipients(ctx: Ctx, acc: AccountInfo) -> None:
    ctx.state.data = drafts.empty_draft(acc)
    ctx.state.set(st.COMPOSE_TO)
    await ctx.send(t("compose.new_from", title=acc.title), keyboard=kb.compose_cancel())


@router.state(st.COMPOSE_TO)
async def compose_to(ctx: Ctx) -> None:
    try:
        to = parse_recipients(ctx.text)
    except ValueError as exc:
        await ctx.send(t("compose.bad_recipients", error=str(exc)), keyboard=kb.compose_cancel())
        return
    ctx.state.update(to=to)
    ctx.state.set(st.COMPOSE_SUBJECT)
    await ctx.send(t("compose.ask_subject"), keyboard=kb.compose_cancel())


@router.state(st.COMPOSE_SUBJECT)
async def compose_subject(ctx: Ctx) -> None:
    ctx.state.update(subject="" if ctx.text == "-" else ctx.text)
    await start_body(ctx, t("compose.new_letter"))


# ============================================================================ набор текста
@router.state(st.COMPOSE_BODY)
async def compose_body(ctx: Ctx) -> None:
    """Добавляет очередное сообщение (текст и/или файлы) в черновик."""
    msg = ctx.message
    data = ctx.state.data
    html_parts: list[str] = list(data.get("html_parts", []))
    text_parts: list[str] = list(data.get("text_parts", []))
    files: list[dict] = list(data.get("files", []))
    warnings: list[str] = []

    # Текст с форматированием ВКонтакте -> HTML письма
    if msg.text:
        html_parts.append(vk_to_html(msg.text, msg.format_data))
        text_parts.append(msg.text)

    limit = ctx.settings.max_attachment_size
    for index, att in enumerate(msg.attachments, start=1):
        info = describe_attachment(att, index)
        if info is None or (not info.get("video") and not info.get("url")):
            warnings.append(t("compose.unsupported"))
            continue
        if info.get("video"):
            warnings.append(t("compose.video_unsupported"))
            continue
        if info["size"] and info["size"] > limit:
            warnings.append(t("compose.file_too_big", name=info["name"], limit=limit // 1024 // 1024))
            continue
        try:
            content = await ctx.api.download(info["url"], max_size=limit)
        except VkApiError as exc:
            logger.warning("VK user=%s: не удалось скачать вложение %s: %s", ctx.user_id, info["name"], exc)
            warnings.append(t("compose.file_failed", name=info["name"], error=str(exc)[:200]))
            continue
        files.append({"name": info["name"], "mime": info["mime"], "size": len(content), "content": content})

    if not msg.text and not msg.attachments:
        warnings.append(t("compose.unsupported"))

    ctx.state.update(html_parts=html_parts, text_parts=text_parts, files=files)
    for warning in dict.fromkeys(warnings):  # без повторов
        await ctx.send(warning)
    await refresh_panel(ctx)


@router.state(st.COMPOSE_EDIT_SUBJECT)
async def edit_subject(ctx: Ctx) -> None:
    ctx.state.update(subject="" if ctx.text == "-" else ctx.text)
    ctx.state.set(st.COMPOSE_BODY)
    await refresh_panel(ctx)


@router.state(st.COMPOSE_EDIT_CC)
async def edit_cc(ctx: Ctx) -> None:
    if ctx.text == "-":
        cc: list[str] = []
    else:
        try:
            cc = parse_recipients(ctx.text)
        except ValueError as exc:
            await ctx.send(t("compose.bad_cc", error=str(exc)))
            return
    ctx.state.update(cc=cc)
    ctx.state.set(st.COMPOSE_BODY)
    await refresh_panel(ctx)


# ============================================================================ кнопки черновика
@router.button("cmp")
async def compose_buttons(ctx: Ctx) -> None:
    if not ctx.state.in_group("compose:"):
        await ctx.answer(t.plain("compose.closed"))
        return
    op = ctx.payload.get("op")

    if op == "cancel":
        ctx.state.clear()
        await ctx.answer(t.plain("compose.cancelled_short"))
        await ctx.edit_current(t("compose.cancelled"))
        return

    if ctx.state.name != st.COMPOSE_BODY:
        await ctx.answer()
        return

    if op == "clear":
        ctx.state.update(html_parts=[], text_parts=[], files=[])
        await ctx.answer(t.plain("compose.cleared"))
        await refresh_panel(ctx)
    elif op == "subject":
        await ctx.answer()
        ctx.state.set(st.COMPOSE_EDIT_SUBJECT)
        await ctx.send(t("compose.ask_new_subject"))
    elif op == "cc":
        await ctx.answer()
        ctx.state.set(st.COMPOSE_EDIT_CC)
        await ctx.send(t("compose.ask_cc"))
    elif op == "preview":
        await ctx.answer()
        await _preview(ctx)
    elif op == "send":
        await _send(ctx)


async def _preview(ctx: Ctx) -> None:
    """Показывает, как будет выглядеть письмо."""
    head, body, tail = drafts.preview_parts(t, ctx.state.data)
    text = head + body + tail
    if len(html_to_vk(text).text) > VK_TEXT_LIMIT:
        text = head + t("compose.preview_too_long") + tail
    await ctx.send(text)
    await refresh_panel(ctx)


async def _send(ctx: Ctx) -> None:
    """Собирает и отправляет письмо."""
    data = ctx.state.data
    if drafts.is_empty(data):
        await ctx.answer(t.plain("compose.empty"))
        return
    if not data.get("to"):
        await ctx.answer(t.plain("compose.no_recipient"))
        return
    acc = await ctx.repo.get_account(ctx.user_id, data["account_id"])
    if acc is None:
        await ctx.answer(t.plain("common.account_not_found"))
        return

    await ctx.answer(t.plain("compose.sending_short"))
    await ctx.edit_current(t("compose.sending"))

    try:
        attachments = [Attachment(filename=f["name"], content=f["content"], content_type=f["mime"])
                       for f in data.get("files", [])]
        await ctx.mail.send(
            acc.credentials,
            to=data["to"],
            cc=data.get("cc") or None,
            subject=data.get("subject", ""),
            text="\n\n".join(data.get("text_parts", [])),
            html_body=chat_html_to_email_html("\n\n".join(data.get("html_parts", []))),
            attachments=attachments,
            in_reply_to=data.get("in_reply_to", ""),
            references=data.get("references", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("VK user=%s: ошибка отправки письма с ящика id=%s: %s", ctx.user_id, acc.id, exc)
        await ctx.edit_current(t("compose.send_failed", error=str(exc)[:300]), keyboard=kb.compose_panel())
        return

    ctx.state.clear()
    logger.info(
        "VK user=%s отправил письмо с ящика id=%s (получателей: %d, вложений: %d)",
        ctx.user_id, acc.id, len(data["to"]) + len(data.get("cc", [])), len(data.get("files", [])),
    )
    await ctx.edit_current(drafts.sent_text(t, acc.title, data))
