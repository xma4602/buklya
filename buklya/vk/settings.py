"""
Настройки бота ВКонтакте (переменные окружения с префиксом VK_).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode

from buklya.config import SETTINGS_CONFIG, split_csv


class VkSettings(BaseSettings):
    model_config = {**SETTINGS_CONFIG, "env_prefix": "VK_"}

    #: VK_TOKEN — ключ доступа сообщества (Управление → Работа с API → Ключи доступа).
    #: Права: управление сообществом (обязательно для Long Poll), сообщения сообщества, документы, фотографии.
    token: str = ""

    #: VK_GROUP_ID — ID сообщества (без минуса). Необязательно — определяется по ключу.
    group_id: int = 0

    #: VK_ALLOWED_USERS — ID пользователей ВКонтакте через запятую (пусто — всем)
    allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    @field_validator("allowed_users", mode="before")
    @classmethod
    def _csv(cls, value):
        return split_csv(value)


@lru_cache
def get_vk_settings() -> VkSettings:
    return VkSettings()


def config_error() -> str | None:
    """Для реестра платформ: чего не хватает, чтобы запустить бота (None — всё задано)."""
    return None if get_vk_settings().token else "не задан VK_TOKEN"
