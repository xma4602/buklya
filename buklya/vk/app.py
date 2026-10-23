"""
Сборка и запуск бота ВКонтакте (vkbottle, Bots Long Poll API).

Что нужно в настройках сообщества ВКонтакте:
    * Сообщения → включить сообщения сообщества;
    * Сообщения → Настройки для бота → включить «Возможности ботов»;
    * Работа с API → Ключи доступа → создать ключ с правами «Управление
      сообществом» (обязательно — без него Bots Long Poll не работает),
      «Сообщения сообщества», «Документы», «Фотографии»;
    * Long Poll API Букля включает сама (версия API, события «Входящее сообщение»
      и «Действие с сообщением»). Сервер с SSL и белый IP не нужны.

Вызывается из точки входа (`buklya.__main__`) — отдельно или вместе с Telegram-ботом.
"""

from __future__ import annotations

import logging
from typing import Any

from vkbottle import API, Bot, BotPolling, GroupEventType

from buklya.app_context import AppContext
from buklya.config import ConfigError
from buklya.vk import PLATFORM
from buklya.vk.api import VkApiError, VkbottleGateway
from buklya.vk.context import VkDeps
from buklya.vk.notifier import VkNotifier
from buklya.vk.router import VkDispatcher
from buklya.vk.settings import VkSettings, get_vk_settings

logger = logging.getLogger(__name__)


class _DropExtraKwargs(logging.Filter):
    """vkbottle на каждую ошибку API пишет «VK API Error N has extra kwargs» — это шум."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "has extra kwargs" not in record.getMessage()


def _quiet_vkbottle() -> None:
    """Убирает служебный шум vkbottle (и в стандартном logging, и в loguru, если он установлен)."""
    logging.getLogger("vkbottle").addFilter(_DropExtraKwargs())
    try:
        from loguru import logger as loguru_logger

        loguru_logger.disable("vkbottle.exception_factory")
    except ImportError:
        pass


def create_app(ctx: AppContext) -> "VkApp":
    """Для реестра платформ (buklya.platforms)."""
    return VkApp(ctx)


class VkApp:
    """Бот ВКонтакте: vkbottle Bot + обработчики Букли."""

    platform = PLATFORM

    def __init__(self, ctx: AppContext, vk_settings: VkSettings | None = None) -> None:
        _quiet_vkbottle()
        settings = ctx.settings
        vk = vk_settings or get_vk_settings()
        self.api = API(vk.token)
        self.polling = BotPolling(self.api, group_id=vk.group_id or None)
        self.bot = Bot(api=self.api, polling=self.polling)
        self.gateway = VkbottleGateway(self.api)
        self.deps = VkDeps(
            api=self.gateway,
            repo=ctx.repo,
            mail=ctx.mail,
            settings=settings,
            providers=ctx.providers,
            allowed_users=vk.allowed_users,
        )
        self.dispatcher = VkDispatcher(self.deps)
        self.notifier = VkNotifier(self.gateway, attach_html=settings.attach_html_original)
        self.group_id = vk.group_id
        self._register()

    def _register(self) -> None:
        """«Сырые» события vkbottle передаются в наш диспетчер."""
        dispatcher = self.dispatcher

        @self.bot.on.raw_event(GroupEventType.MESSAGE_NEW, dict)
        async def on_message(event: dict[str, Any]) -> None:
            await dispatcher.feed_message(event.get("object") or {})

        @self.bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dict)
        async def on_button(event: dict[str, Any]) -> None:
            await dispatcher.feed_event(event.get("object") or {})

    async def start(self) -> None:
        """Проверяет токен сообщества и настраивает Long Poll (до запуска получения событий)."""
        response = (await self.api.request("groups.getById", {}))["response"]
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not groups:
            raise RuntimeError("VK_TOKEN не является ключом доступа сообщества")
        group = groups[0]
        self.group_id = self.group_id or int(group["id"])
        self.polling.group_id = self.group_id
        logger.info("ВКонтакте: сообщество «%s» (id=%s) авторизовано", group.get("name"), self.group_id)
        await self._ensure_long_poll()

    async def _ensure_long_poll(self) -> None:
        """
        Включает Long Poll и события message_new / message_event и проверяет,
        что сервер Long Poll доступен с этим ключом.

        Для Bots Long Poll API ключу сообщества ОБЯЗАТЕЛЬНО нужно право
        «Управление сообществом» (manage): без него ВКонтакте не выдаёт сервер
        (ошибка 15, subcode 1133). В этом случае запуск останавливается с подсказкой.
        """
        try:
            await self.gateway.call(
                "groups.setLongPollSettings",
                group_id=self.group_id,
                enabled=1,
                api_version=self.api.API_VERSION,
                message_new=1,
                message_event=1,
            )
            logger.info("ВКонтакте: Long Poll API включён (message_new, message_event)")
        except VkApiError as exc:
            if exc.code != 15:  # нет прав — ниже будет понятная ошибка
                logger.warning("ВКонтакте: не удалось настроить Long Poll автоматически: %s", exc)

        try:
            await self.gateway.call("groups.getLongPollServer", group_id=self.group_id)
        except VkApiError as exc:
            if exc.code == 15:
                raise ConfigError(
                    "у ключа доступа ВКонтакте (VK_TOKEN) нет права «Управление сообществом» — без него "
                    "Long Poll не работает. Создайте новый ключ: сообщество → Управление → Работа с API → "
                    "Ключи доступа → Создать ключ, отметьте «Управление сообществом», «Сообщения сообщества», "
                    "«Документы», «Фотографии» и впишите его в VK_TOKEN"
                ) from exc
            if exc.code == 100:
                raise ConfigError(
                    "Long Poll API в сообществе выключен. Включите: Управление → Работа с API → Long Poll API "
                    f"(версия {self.api.API_VERSION}), типы событий «Входящее сообщение» и «Действие с сообщением»"
                ) from exc
            raise

    async def run(self) -> None:
        """Получение событий (работает до отмены задачи)."""
        await self.bot.run_polling()

    async def close(self) -> None:
        http = getattr(self.api, "http_client", None)
        if http is not None and hasattr(http, "close"):
            await http.close()
