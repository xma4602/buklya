"""
Маршрутизация событий бота ВКонтакте.

vkbottle отдаёт «сырые» события message_new и message_event, а дальше они
разбираются здесь — по тем же правилам, что и в Telegram-боте:

    1. команды и кнопки главного меню (они же сбрасывают текущий диалог);
    2. обработчик текущего шага диалога (подключение ящика, набор письма…);
    3. «не понимаю».

Нажатия callback-кнопок находятся по полям payload: {"a": раздел, "op": действие}.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from buklya.vk import keyboards as kb
from buklya.vk.context import ButtonEvent, Ctx, Incoming, VkDeps
from buklya.vk.texts import t

logger = logging.getLogger(__name__)
action_logger = logging.getLogger("buklya.actions")

Handler = Callable[[Ctx], Awaitable[Any]]



def command_aliases() -> dict[str, str]:
    """
    Словесные синонимы команд из локализации (vk.aliases.<команда>: «слово, слово»).
    Кнопка «Начать» во ВКонтакте присылает payload {"command": "start"} — она обрабатывается отдельно.
    """
    result: dict[str, str] = {}
    for command in ("start", "help", "cancel"):
        if t.has(f"aliases.{command}"):
            for word in t.plain(f"aliases.{command}").split(","):
                if word.strip():
                    result[word.strip().lower()] = command
    return result


class VkRouter:
    """Реестр обработчиков: команды, шаги диалогов, callback-кнопки."""

    def __init__(self) -> None:
        self.commands: dict[str, Handler] = {}
        self.states: dict[str, Handler] = {}
        self.buttons: dict[tuple[str, str | None], Handler] = {}

    # ------------------------------------------------------------------ регистрация
    def command(self, *names: str) -> Callable[[Handler], Handler]:
        """Команда: «/name», кнопка меню с payload {"cmd": name} или её подпись."""

        def decorator(func: Handler) -> Handler:
            for name in names:
                self.commands[name] = func
            return func

        return decorator

    def state(self, *names: str) -> Callable[[Handler], Handler]:
        """Сообщение пользователя на шаге диалога `name`."""

        def decorator(func: Handler) -> Handler:
            for name in names:
                self.states[name] = func
            return func

        return decorator

    def button(self, section: str, *ops: str) -> Callable[[Handler], Handler]:
        """Callback-кнопка с payload {"a": section, "op": op}. Без ops — любые действия раздела."""

        def decorator(func: Handler) -> Handler:
            for op in ops or (None,):
                self.buttons[(section, op)] = func
            return func

        return decorator

    # ------------------------------------------------------------------ поиск обработчика
    def command_of(self, msg: Incoming, in_dialog: bool = False) -> str | None:
        """
        Имя команды во входящем сообщении (или None).

        :param in_dialog: пользователь сейчас что-то вводит (например, текст письма) —
            тогда словесные синонимы («помощь», «начать») не считаются командами,
            чтобы не прервать набор письма. «/команды» и кнопки меню работают всегда.
        """
        if msg.payload.get("cmd") in self.commands:
            return msg.payload["cmd"]
        if msg.payload.get("command") == "start":
            return "start"
        text = msg.text.strip()
        if text.startswith("/"):
            name = text[1:].split()[0].split("@")[0].lower() if len(text) > 1 else ""
            return name if name in self.commands else None
        alias = command_aliases().get(text.lower())
        if alias and (not in_dialog or alias == "cancel"):
            return alias
        for cmd, key in kb.MENU.items():  # подпись кнопки меню, набранная вручную
            if text == t.plain(key):
                return cmd
        return None

    def button_handler(self, payload: dict[str, Any]) -> Handler | None:
        section = str(payload.get("a") or "")
        op = payload.get("op")
        return self.buttons.get((section, op)) or self.buttons.get((section, None))


#: Общий реестр — модули из buklya.vk.handlers регистрируют в нём свои обработчики
router = VkRouter()


class VkDispatcher:
    """Принимает события ВКонтакте и вызывает обработчики."""

    def __init__(self, deps: VkDeps, vk_router: VkRouter | None = None) -> None:
        # Импорт регистрирует обработчики в router
        import buklya.vk.handlers  # noqa: F401

        self.deps = deps
        self.router = vk_router or router
        self.allowed = set(deps.allowed_users)

    def _allowed(self, vk_user_id: int) -> bool:
        return not self.allowed or vk_user_id in self.allowed

    # ------------------------------------------------------------------ message_new
    async def feed_message(self, obj: dict[str, Any]) -> None:
        """Событие message_new (поле object)."""
        msg = Incoming.from_event(obj)
        if msg.peer_id != msg.from_id or msg.from_id <= 0:
            return  # бот работает только в личных сообщениях сообщества
        ctx = Ctx(self.deps, msg.from_id, msg.peer_id, message=msg)

        if not self._allowed(msg.from_id):
            action_logger.warning("VK: доступ запрещён: vk_id=%s", msg.from_id)
            await ctx.send(t("common.access_denied"))
            return

        async with self.deps.states.lock(msg.from_id):
            await ctx.resolve_user()
            command = self.router.command_of(msg, in_dialog=ctx.state.name is not None)
            what = f"команда {command}" if command else (
                "сообщение (" + ", ".join(a.get("type", "?") for a in msg.attachments) + ")"
                if msg.attachments else "сообщение (text)"
            )
            action_logger.info("VK user=%s state=%s: %s", ctx.user_id, ctx.state.name, what)
            try:
                if command:
                    await self.router.commands[command](ctx)
                elif ctx.state.name and ctx.state.name in self.router.states:
                    await self.router.states[ctx.state.name](ctx)
                else:
                    await ctx.send(t("common.unknown_message"), keyboard=kb.main_menu())
            except Exception:  # noqa: BLE001 — одна запись в логе; бот продолжает работать
                logger.exception("VK: ошибка при обработке сообщения от user=%s", ctx.user_id)

    # ------------------------------------------------------------------ message_event
    async def feed_event(self, obj: dict[str, Any]) -> None:
        """Событие message_event — нажатие callback-кнопки (поле object)."""
        event = ButtonEvent.from_event(obj)
        ctx = Ctx(self.deps, event.user_id, event.peer_id, event=event)

        if not self._allowed(event.user_id):
            action_logger.warning("VK: доступ запрещён: vk_id=%s", event.user_id)
            await ctx.answer(t.plain("common.access_denied_short"))
            return

        async with self.deps.states.lock(event.user_id):
            await ctx.resolve_user()
            action_logger.info("VK user=%s state=%s: кнопка %s", ctx.user_id, ctx.state.name, event.payload)
            handler = self.router.button_handler(event.payload)
            try:
                if handler is None:
                    await ctx.answer(t.plain("common.stale_button"))
                else:
                    await handler(ctx)
            except Exception:  # noqa: BLE001 — одна запись в логе; бот продолжает работать
                logger.exception("VK: ошибка при обработке кнопки от user=%s", ctx.user_id)
            finally:
                await ctx.answer()  # если обработчик сам не ответил на нажатие
