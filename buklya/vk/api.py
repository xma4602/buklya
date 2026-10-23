"""
Обёртка над API ВКонтакте.

Обработчики бота работают не с vkbottle напрямую, а с этим небольшим
интерфейсом (`VkGateway`). Это:
    * изолирует остальной код от деталей библиотеки;
    * позволяет в тестах подменить API фейком без сети.

Реализация `VkbottleGateway` использует vkbottle.API (лимиты запросов, повтор
при ошибках сети) и загрузчики файлов vkbottle.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from buklya.services.notifier import RecipientUnavailable
from buklya.vk.markup import VkText, truncate

logger = logging.getLogger(__name__)

#: Коды ошибок API, означающие «писать этому пользователю нельзя»:
#: 7 — нет прав, 900 — пользователь в чёрном списке, 901 — пользователь запретил
#: сообщения от сообщества, 902 — настройки приватности.
UNAVAILABLE_CODES = {7, 900, 901, 902}

#: 912 — в сообществе выключены «Возможности ботов» (клавиатуры и кнопки недоступны)
BOT_FEATURES_DISABLED = 912

#: Подсказка администратору, если «Возможности ботов» выключены
BOT_FEATURES_HINT = (
    "ВКонтакте: в сообществе выключены «Возможности ботов», поэтому кнопки не работают и сообщения "
    "отправляются без клавиатуры. Включите: сообщество → Управление → Сообщения → Настройки для бота → "
    "«Возможности ботов: Включены» (и при желании «Кнопка „Начать“»). Перезапуск бота не нужен."
)

#: Пустая inline-клавиатура: передаётся при редактировании, чтобы убрать кнопки
EMPTY_INLINE_KEYBOARD = '{"inline": true, "buttons": []}'


class VkApiError(Exception):
    """Ошибка API ВКонтакте (текст можно показать пользователю)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class VkUserUnavailable(VkApiError, RecipientUnavailable):
    """Пользователь запретил сообщения от сообщества / заблокировал бота."""


class VkGateway(Protocol):
    """Операции с API ВКонтакте, которые нужны боту."""

    async def send(self, peer_id: int, message: VkText, *, keyboard: str | None = None,
                   attachment: str | None = None) -> int:
        """Отправляет сообщение. Возвращает conversation_message_id (для редактирования)."""

    async def edit(self, peer_id: int, cmid: int, message: VkText, *, keyboard: str | None = None) -> None:
        """Редактирует сообщение бота."""

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str | None = None) -> None:
        """Ответ на нажатие callback-кнопки (text — всплывающее уведомление)."""

    async def delete(self, peer_id: int, cmid: int) -> bool:
        """Удаляет сообщение у всех. False — если ВКонтакте не разрешил."""

    async def upload_doc(self, peer_id: int, data: bytes, filename: str) -> str:
        """Загружает файл как документ и возвращает строку вложения («doc…»)."""

    async def upload_photo(self, peer_id: int, data: bytes, filename: str) -> str:
        """Загружает картинку как фото и возвращает строку вложения («photo…»)."""

    async def download(self, url: str, max_size: int | None = None) -> bytes:
        """Скачивает файл из ВКонтакте (вложение сообщения пользователя)."""

    async def first_name(self, user_id: int) -> str | None:
        """Имя пользователя (для приветствия); None — если узнать не удалось."""


def _wrap_error(exc: Exception) -> VkApiError:
    """Исключение vkbottle -> VkApiError / VkUserUnavailable."""
    code = int(getattr(exc, "code", 0) or 0)
    message = getattr(exc, "error_msg", None) or str(exc) or exc.__class__.__name__
    if code in UNAVAILABLE_CODES:
        return VkUserUnavailable(message, code)
    return VkApiError(message, code)


class VkbottleGateway:
    """Реализация `VkGateway` на vkbottle."""

    def __init__(self, api: Any) -> None:  # vkbottle.API
        from vkbottle import DocMessagesUploader, PhotoMessageUploader

        self.api = api
        self._docs = DocMessagesUploader(api)
        self._photos = PhotoMessageUploader(api)
        self._features_warned = False

    def _warn_features(self) -> None:
        """Подсказка про «Возможности ботов» — один раз, чтобы не засорять лог."""
        if not self._features_warned:
            self._features_warned = True
            logger.error(BOT_FEATURES_HINT)

    async def call(self, method: str, **params: Any) -> Any:
        """Вызов метода API; пустые параметры не передаются."""
        params = {key: value for key, value in params.items() if value is not None}
        try:
            response = await self.api.request(method, params)
        except Exception as exc:  # noqa: BLE001 — vkbottle бросает много разных исключений
            raise _wrap_error(exc) from exc
        return response.get("response") if isinstance(response, dict) else response

    async def send(self, peer_id: int, message: VkText, *, keyboard: str | None = None,
                   attachment: str | None = None) -> int:
        message = truncate(message)
        # peer_ids (а не peer_id) — тогда ВКонтакте возвращает conversation_message_id,
        # по которому сообщения сообщества можно редактировать
        params = dict(
            peer_ids=str(peer_id),
            random_id=0,
            message=message.text or None,
            format_data=message.format_data,
            keyboard=keyboard,
            attachment=attachment,
            dont_parse_links=1,
        )
        try:
            result = await self.call("messages.send", **params)
        except VkApiError as exc:
            if exc.code != BOT_FEATURES_DISABLED or not keyboard:
                raise
            # Клавиатуры недоступны — отправляем хотя бы текст
            self._warn_features()
            params["keyboard"] = None
            result = await self.call("messages.send", **params)
        if isinstance(result, list) and result:
            item = result[0]
            if item.get("error"):
                error = item["error"]
                raise _wrap_error(VkApiError(error.get("description") or str(error), int(error.get("code", 0))))
            return int(item.get("conversation_message_id") or 0)
        return 0

    async def edit(self, peer_id: int, cmid: int, message: VkText, *, keyboard: str | None = None) -> None:
        message = truncate(message)
        params = dict(
            peer_id=peer_id,
            conversation_message_id=cmid,
            message=message.text or None,
            format_data=message.format_data,
            keyboard=keyboard if keyboard is not None else EMPTY_INLINE_KEYBOARD,
            dont_parse_links=1,
            keep_forward_messages=1,
        )
        try:
            await self.call("messages.edit", **params)
        except VkApiError as exc:
            if exc.code != BOT_FEATURES_DISABLED:
                raise
            self._warn_features()
            params["keyboard"] = None
            await self.call("messages.edit", **params)

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str | None = None) -> None:
        event_data = json.dumps({"type": "show_snackbar", "text": text[:90]}, ensure_ascii=False) if text else None
        try:
            await self.call(
                "messages.sendMessageEventAnswer",
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data=event_data,
            )
        except VkApiError as exc:  # ответ на кнопку — не критично (например, истёк срок события)
            logger.debug("sendMessageEventAnswer: %s", exc)

    async def delete(self, peer_id: int, cmid: int) -> bool:
        try:
            await self.call("messages.delete", peer_id=peer_id, cmids=str(cmid), delete_for_all=1)
            return True
        except VkApiError as exc:
            logger.info("Не удалось удалить сообщение peer=%s cmid=%s: %s", peer_id, cmid, exc)
            return False

    async def upload_doc(self, peer_id: int, data: bytes, filename: str) -> str:
        try:
            return await self._docs.upload(file_source=data, peer_id=peer_id, title=filename)
        except Exception as exc:  # noqa: BLE001
            raise _wrap_error(exc) from exc

    async def upload_photo(self, peer_id: int, data: bytes, filename: str) -> str:
        try:
            return await self._photos.upload(file_source=data, peer_id=peer_id)
        except Exception as exc:  # noqa: BLE001
            raise _wrap_error(exc) from exc

    async def download(self, url: str, max_size: int | None = None) -> bytes:
        try:
            data = await self.api.http_client.request_content(url)
        except Exception as exc:  # noqa: BLE001
            raise VkApiError(str(exc) or exc.__class__.__name__) from exc
        if max_size and len(data) > max_size:
            raise VkApiError(f"{len(data) // 1024 // 1024} MB > {max_size // 1024 // 1024} MB")
        return data

    async def first_name(self, user_id: int) -> str | None:
        try:
            users = await self.call("users.get", user_ids=str(user_id))
        except VkApiError:
            return None
        return (users[0].get("first_name") or None) if users else None
