"""
Просмотр почты (ВКонтакте): список последних писем, открытие письма, отметка «прочитано».

Во ВКонтакте в inline-клавиатуре помещается не больше 10 кнопок, поэтому
список писем печатается текстом с номерами, а кнопки — это номера писем
и навигация «Новее / 🔄 / Старее».
"""

from __future__ import annotations

import logging

from buklya.db.repository import AccountInfo
from buklya.i18n import Safe
from buklya.mail.models import MessageHeader
from buklya.vk import keyboards as kb
from buklya.vk.context import Ctx
from buklya.vk.render import send_email
from buklya.vk.router import router
from buklya.vk.texts import t

logger = logging.getLogger(__name__)


def page_size(ctx: Ctx) -> int:
    """Писем на странице: из настроек, но не больше, чем помещается кнопок."""
    return max(1, min(ctx.settings.last_messages_count, kb.MAX_PAGE_SIZE))


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def page_text(acc: AccountInfo, headers: list[MessageHeader], offset: int, limit: int, total: int) -> Safe:
    """Текст страницы: заголовок + пронумерованный список писем."""
    first, last = offset + 1, min(offset + limit, total)
    lines = [t("inbox.page", title=acc.title, first=first, last=last, total=total), ""]
    for index, h in enumerate(headers):
        lines.append(
            t(
                "inbox.page_item",
                number=offset + index + 1,
                unread=t.plain("inbox.unread_mark") if not h.seen else "",
                sender=_short(h.from_, 30),
                subject=_short(h.subject, 60),
            )
        )
    return Safe("\n".join(lines))


async def show_page(ctx: Ctx, acc: AccountInfo, offset: int, cmid: int = 0) -> None:
    """Показывает (или обновляет сообщение `cmid`) страницу списка писем."""
    limit = page_size(ctx)
    if not cmid:
        cmid = await ctx.send(t("inbox.loading", title=acc.title))
    try:
        headers, total, _ = await ctx.mail.recent(acc.credentials, offset, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось получить список писем: %s", acc.id, exc)
        await ctx.edit(cmid, t("inbox.load_failed", title=acc.title, error=str(exc)[:300]))
        return

    if total == 0:
        await ctx.edit(cmid, t("inbox.empty", title=acc.title))
        return

    await ctx.edit(
        cmid,
        page_text(acc, headers, offset, limit, total),
        keyboard=kb.messages_page(acc.id, headers, offset, limit, total),
    )


@router.command("last")
async def last_messages(ctx: Ctx) -> None:
    """Последние письма: сразу список, если ящик один, иначе выбор ящика."""
    ctx.state.clear()
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    if not accounts:
        await ctx.send(t("common.need_account"))
        return
    if len(accounts) == 1:
        await show_page(ctx, accounts[0], 0)
        return
    await ctx.send(t("inbox.choose_account"), keyboard=kb.choose_account(accounts, "last"))


async def _account_from_payload(ctx: Ctx) -> AccountInfo | None:
    acc = await ctx.repo.get_account(ctx.user_id, int(ctx.payload.get("id") or 0))
    if acc is None:
        await ctx.answer(t.plain("common.account_not_found"))
    return acc


@router.button("acc", "last")
async def last_for_account(ctx: Ctx) -> None:
    acc = await _account_from_payload(ctx)
    if acc is None:
        return
    await ctx.answer()
    await ctx.edit_current(t("inbox.loading", title=acc.title))
    await show_page(ctx, acc, 0, cmid=ctx.event.cmid)


@router.button("pg")
async def page(ctx: Ctx) -> None:
    """Листание списка писем."""
    acc = await _account_from_payload(ctx)
    if acc is None:
        return
    await ctx.answer(t.plain("common.loading"))
    await show_page(ctx, acc, max(int(ctx.payload.get("o") or 0), 0), cmid=ctx.event.cmid)


@router.button("op")
async def open_message(ctx: Ctx) -> None:
    """Открывает письмо целиком."""
    acc = await _account_from_payload(ctx)
    if acc is None:
        return
    uid = int(ctx.payload.get("u") or 0)
    await ctx.answer(t.plain("inbox.opening"))
    try:
        email = await ctx.mail.fetch(acc.credentials, uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ящик id=%s: не удалось открыть письмо uid=%s: %s", acc.id, uid, exc)
        await ctx.send(t("inbox.open_failed", error=str(exc)[:300]))
        return
    if email is None:
        await ctx.send(t("inbox.not_found"))
        return

    ref_id = await ctx.repo.save_message(acc, email.uid, acc.uid_validity, email.reply_meta())
    logger.info("VK user=%s открыл письмо uid=%s из ящика id=%s", ctx.user_id, email.uid, acc.id)
    await send_email(
        ctx.api,
        ctx.peer_id,
        account_title=acc.title,
        email=email,
        ref_id=ref_id,
        title=t.plain("email.opened"),
        attach_html=ctx.settings.attach_html_original,
    )


@router.button("msg", "seen")
async def mark_seen(ctx: Ctx) -> None:
    """Помечает письмо прочитанным на сервере."""
    found = await ctx.repo.get_message(ctx.user_id, int(ctx.payload.get("r") or 0))
    if found is None:
        await ctx.answer(t.plain("common.message_not_found"))
        return
    ref, acc = found
    try:
        await ctx.mail.mark_seen(acc.credentials, ref.uid)
    except Exception as exc:  # noqa: BLE001
        await ctx.answer(t.plain("common.error", error=str(exc)[:80]))
        return
    await ctx.answer(t.plain("inbox.marked_seen"))
