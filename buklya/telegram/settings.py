"""
Настройки Telegram-бота (переменные окружения с префиксом TELEGRAM_).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode

from buklya.config import SETTINGS_CONFIG, split_csv


class TelegramSettings(BaseSettings):
    model_config = {**SETTINGS_CONFIG, "env_prefix": "TELEGRAM_"}

    #: TELEGRAM_TOKEN — токен бота от @BotFather
    token: str = ""

    #: TELEGRAM_ALLOWED_USERS — Telegram ID через запятую, кому можно пользоваться ботом
    #: (пусто — всем)
    allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    @field_validator("allowed_users", mode="before")
    @classmethod
    def _csv(cls, value):
        return split_csv(value)


@lru_cache
def get_telegram_settings() -> TelegramSettings:
    return TelegramSettings()


def config_error() -> str | None:
    """Для реестра платформ: чего не хватает, чтобы запустить бота (None — всё задано)."""
    return None if get_telegram_settings().token else "не задан TELEGRAM_TOKEN"
