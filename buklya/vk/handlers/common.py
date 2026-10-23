"""
Общие команды: начать, справка, отмена.
"""

from __future__ import annotations

from buklya.vk import keyboards as kb
from buklya.vk.context import Ctx
from buklya.vk.router import router
from buklya.vk.texts import t


@router.command("start")
async def cmd_start(ctx: Ctx) -> None:
    """Приветствие и регистрация пользователя."""
    ctx.state.clear()
    await ctx.repo.ensure_user(ctx.user_id)
    accounts = await ctx.repo.list_accounts(ctx.user_id)
    name = await ctx.api.first_name(ctx.vk_user_id) or t.plain("common.friend")
    text = t("common.start", name=name) + "\n\n"
    if not accounts:
        text += t("common.start_no_accounts")
    else:
        text += t("common.start_has_accounts", count=len(accounts))
    await ctx.send(text, keyboard=kb.main_menu())


@router.command("help")
async def cmd_help(ctx: Ctx) -> None:
    """Справка."""
    await ctx.send(t("help.text"), keyboard=kb.main_menu())


@router.command("cancel")
async def cmd_cancel(ctx: Ctx) -> None:
    """Отмена любого диалога (подключение ящика, написание письма и т.п.)."""
    current = ctx.state.name
    ctx.state.clear()
    await ctx.send(t("common.cancelled" if current else "common.nothing_to_cancel"), keyboard=kb.main_menu())
