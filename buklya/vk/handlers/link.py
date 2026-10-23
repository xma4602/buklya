"""
Связка мессенджеров (ВКонтакте ↔ Telegram и др.): общие ящики для одного человека.

/link        — показать связанные мессенджеры и код для связки;
/link КОД    — ввести код, полученный в другом мессенджере.
Логика — в `buklya.services.linking`, здесь только интерфейс ВКонтакте.
"""

from __future__ import annotations

import logging

from buklya.i18n import Safe
from buklya.services import linking as lk
from buklya.vk import PLATFORM
from buklya.vk import keyboards as kb
from buklya.vk import states as st
from buklya.vk.context import Ctx
from buklya.vk.router import router
from buklya.vk.texts import t

logger = logging.getLogger(__name__)


async def _screen(ctx: Ctx) -> tuple[Safe, str]:
    """Текст и кнопки экрана «Мессенджеры» (каждый показ — новый код)."""
    identities = await ctx.linking.identities(ctx.user_id)
    code, _ = await ctx.linking.issue_code(ctx.user_id)
    text = lk.screen_text(t, identities, code, int(ctx.linking.ttl.total_seconds() // 60))
    return text, kb.link_menu(can_unlink=len(identities) > 1)


async def _redeem(ctx: Ctx, code: str) -> None:
    ctx.state.clear()
    result = await ctx.linking.redeem(PLATFORM, ctx.vk_user_id, code)
    if result.status == "ok":
        logger.info("ВКонтакте %s связан с пользователем id=%s", ctx.vk_user_id, result.user_id)
        ctx.user_id = result.user_id
    identities = await ctx.linking.identities(result.user_id) if result.user_id else []
    accounts = len(await ctx.repo.list_accounts(result.user_id)) if result.user_id else 0
    await ctx.send(lk.done_text(t, result, identities, accounts), keyboard=kb.main_menu())


@router.command("link")
async def link_start(ctx: Ctx) -> None:
    """Экран «Мессенджеры» или сразу ввод кода (/link КОД)."""
    parts = ctx.text.split(maxsplit=1)
    if ctx.text.startswith("/") and len(parts) == 2:
        await _redeem(ctx, parts[1])
        return
    ctx.state.clear()
    text, keyboard = await _screen(ctx)
    await ctx.send(text, keyboard=keyboard)


@router.state(st.LINK_CODE)
async def link_code(ctx: Ctx) -> None:
    await _redeem(ctx, ctx.text)


@router.button("lnk")
async def link_buttons(ctx: Ctx) -> None:
    op = ctx.payload.get("op")
    if op == "cancel":
        await ctx.answer()
        ctx.state.clear()
        await ctx.edit_current(t("common.cancelled"))
    elif op == "enter":
        await ctx.answer()
        ctx.state.clear()
        ctx.state.set(st.LINK_CODE)
        await ctx.send(t("link.ask_code"), keyboard=kb.link_cancel())
    elif op == "code":
        await ctx.answer()
        text, keyboard = await _screen(ctx)
        await ctx.edit_current(text, keyboard=keyboard)
    elif op == "unlink":
        identities = await ctx.linking.identities(ctx.user_id)
        if len(identities) < 2:
            await ctx.answer(t.plain("link.nothing_to_unlink"))
            return
        await ctx.answer()
        ctx.state.clear()
        await ctx.linking.unlink(PLATFORM, ctx.vk_user_id)
        rest = [i for i in identities if not (i.platform == PLATFORM and i.external_id == str(ctx.vk_user_id))]
        await ctx.edit_current(t("link.unlinked", list=lk.platforms_list(t, rest)))
