"""
Конфигурация приложения.

Все настройки читаются из переменных окружения и/или файла `.env`
(см. `.env.example`). Для чтения используется pydantic-settings, поэтому
типы автоматически проверяются и приводятся при старте.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from buklya.platforms import parse_platforms

# Корень проекта (папка, где лежат Dockerfile, .env и т.д.)
BASE_DIR = Path(__file__).resolve().parent.parent

#: Общие параметры чтения настроек (используются и настройками платформ)
SETTINGS_CONFIG = SettingsConfigDict(
    env_file=BASE_DIR / ".env",
    env_file_encoding="utf-8",
    extra="ignore",
    # Пустые значения (VK_GROUP_ID=) считаются незаданными — берётся значение по умолчанию
    env_ignore_empty=True,
)


def split_csv(value):
    """Список из строки через запятую: `1,2,3` (для полей-списков в настройках)."""
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class ConfigError(Exception):
    """Ошибка настройки (нет токена, неизвестная платформа и т.п.) — текст для человека."""


class Settings(BaseSettings):
    """Настройки приложения. Имена полей = имена переменных окружения (без учёта регистра)."""

    model_config = SETTINGS_CONFIG

    # ------------------------------------------------------------------ Какие боты запускать
    #: Платформы через запятую (telegram, vk) или all.
    #: Пусто — запускаются все платформы, для которых заданы настройки (токен).
    #: Можно переопределить аргументом командной строки: python -m buklya vk
    #: Настройки самих платформ (токены, списки доступа) — в buklya/<платформа>/settings.py.
    bots: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------ Связка мессенджеров
    #: Сколько минут действует код для связки мессенджеров (/link)
    link_code_ttl_minutes: int = 10

    # ------------------------------------------------------------------ Локализация
    #: Язык текстов бота: файл locales/<язык>.yaml (недостающие строки берутся из ru.yaml)
    locale: str = "ru"

    #: Каталог с файлами локализации
    locales_dir: Path = BASE_DIR / "locales"

    # ------------------------------------------------------------------ База данных
    #: URL базы данных в формате SQLAlchemy (async-драйвер).
    #: SQLite:   sqlite+aiosqlite:///data/buklya.db
    #: Postgres: postgresql+asyncpg://user:pass@host:5432/buklya
    database_url: str = "sqlite+aiosqlite:///data/buklya.db"

    #: Выводить SQL-запросы в лог (для отладки)
    database_echo: bool = False

    # ------------------------------------------------------------------ Безопасность
    #: Мастер-ключ шифрования учётных данных (Fernet-ключ, base64, 32 байта).
    #: Хранится ТОЛЬКО в окружении, в БД не попадает. Сгенерировать:
    #:   python -m buklya.crypto
    encryption_key: str = Field(..., description="Мастер-ключ шифрования")

    #: Старые ключи (через запятую) — для плавной ротации мастер-ключа
    old_encryption_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------ Почта
    #: Как часто (в секундах) проверять новые письма
    poll_interval: int = 60

    #: Сколько ящиков проверять одновременно
    poll_concurrency: int = 5

    #: Тайм-аут сетевых операций с почтовыми серверами (сек.)
    mail_timeout: int = 30

    #: Сколько писем показывать на одной странице «последних писем»
    last_messages_count: int = 10

    #: Максимальный размер вложения, которое бот пересылает в Telegram (байт).
    #: Ограничение Bot API на отправку файла — 50 МБ.
    max_attachment_size: int = 49 * 1024 * 1024

    #: Прикладывать к сообщению исходную HTML-версию письма файлом
    attach_html_original: bool = True

    #: Путь к файлу с пресетами почтовых провайдеров
    providers_file: Path = BASE_DIR / "providers.yaml"

    # ------------------------------------------------------------------ Логирование
    #: Каталог для лог-файлов
    log_dir: Path = BASE_DIR / "logs"

    #: Уровень логирования: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

    #: Сколько дней хранить старые лог-файлы (0 — не удалять)
    log_retention_days: int = 30

    #: Цвет в консоли: auto (если консоль поддерживает), true, false. В файлы цвет не пишется.
    log_color: str = "auto"

    # ------------------------------------------------------------------ Валидаторы
    @field_validator("bots", mode="before")
    @classmethod
    def _parse_bots(cls, value):
        """BOTS=telegram,vk | all | (пусто)."""
        return parse_platforms(value) if value not in (None, "") else []

    @field_validator("old_encryption_keys", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """Позволяет задавать списки строкой через запятую."""
        return split_csv(value)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("log_dir", "providers_file", "locales_dir")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """Относительные пути считаем от корня проекта, а не от текущей папки запуска."""
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """
        Для SQLite с относительным путём (sqlite+aiosqlite:///data/buklya.db)
        файл базы кладётся относительно корня проекта — независимо от того,
        из какой папки запущено приложение.
        """
        prefix = "sqlite+aiosqlite:///"
        if value.startswith(prefix):
            path = value[len(prefix):]
            if path and path != ":memory:" and not Path(path).is_absolute():
                return prefix + (BASE_DIR / path).resolve().as_posix()
        return value


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (кэшируется)."""
    return Settings()  # type: ignore[call-arg]
