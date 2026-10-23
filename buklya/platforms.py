"""
Реестр платформ (мессенджеров), через которые работает Букля.

Ядро (база, почта, поллер) о конкретных мессенджерах не знает: оно работает
с «пользователем Букли», к которому привязаны аккаунты мессенджеров
(таблица identities: платформа + ID пользователя на платформе).

Как добавить новую платформу (например, Matrix):
    1. создать пакет `buklya/<имя>/` по образцу `buklya/vk/`;
    2. в нём два модуля:
         settings.py: def config_error() -> str | None  — чего не хватает в настройках
                                                         (None — платформу можно запускать);
         app.py:      def create_app(ctx: AppContext)    — объект по интерфейсу `BotApp`
                                                         (с уведомителем `Notifier` для почты);
    3. добавить строку в `PLATFORMS` ниже;
    4. (по желанию) секцию `<имя>:` в locales/ru.yaml и название в `platforms.<имя>`;
    5. сервис в docker-compose.yml.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol


@dataclass(frozen=True)
class PlatformSpec:
    """Описание платформы: имя (как в настройках/CLI/БД) и пакет с её кодом."""

    name: str
    package: str


#: Все известные платформы. Порядок — порядок запуска в одном процессе.
PLATFORMS: dict[str, PlatformSpec] = {
    spec.name: spec
    for spec in (
        PlatformSpec("telegram", "buklya.telegram"),
        PlatformSpec("vk", "buklya.vk"),
    )
}


class BotApp(Protocol):
    """Интерфейс бота платформы (см. telegram/app.py, vk/app.py)."""

    #: Имя платформы из PLATFORMS
    platform: str
    #: Уведомитель для фоновой проверки почты
    notifier: Any

    async def start(self) -> None: ...  # проверка токена, подготовка

    async def run(self) -> None: ...  # получение событий до отмены задачи

    async def close(self) -> None: ...  # освобождение ресурсов


def config_error(name: str) -> str | None:
    """Чего не хватает в настройках платформы (None — всё задано). Библиотеку мессенджера не импортирует."""
    return importlib.import_module(f"{PLATFORMS[name].package}.settings").config_error()


def load_app(name: str) -> ModuleType:
    """Модуль запуска платформы (импорт ленивый: библиотеки ненужных платформ не грузятся)."""
    return importlib.import_module(f"{PLATFORMS[name].package}.app")


def parse_platforms(value: str | list[str] | None) -> list[str]:
    """
    Разбирает список платформ: "telegram,vk", "all", ["vk"] и т.п.

    :raises ValueError: при неизвестном имени платформы
    """
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if not name:
            continue
        if name == "all":
            return list(PLATFORMS)
        if name not in PLATFORMS:
            known = ", ".join(PLATFORMS)
            raise ValueError(f"Неизвестная платформа {name!r}, доступны: {known}, all")
        if name not in result:
            result.append(name)
    return result
