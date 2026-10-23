"""
Локализация: все тексты бота лежат в `locales/<язык>.yaml`.

Использование:

    from buklya.i18n import Translator

    t = Translator("telegram")            # тексты для конкретной платформы
    t("accounts.duplicate", email=email)  # -> безопасный HTML (значения экранированы)
    t.plain("mail.no_address")            # -> строка без экранирования (для текстов ошибок)

Особенности:
    * ключи вложенные, обращение через точку: "compose.btn_send";
    * для платформы сначала ищется ключ `<платформа>.<ключ>` (секции `telegram:` и
      `vk:` в YAML), затем общий `<ключ>` — так одна строка может отличаться
      на разных платформах;
    * подставляемые значения экранируются для HTML, кроме обёрнутых в `Safe`
      (уже готовые HTML-фрагменты);
    * если ключа нет в выбранном языке — берётся из языка по умолчанию (ru),
      если нет и там — возвращается сам ключ в квадратных скобках (и пишется в лог).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Каталог с файлами локализации по умолчанию (<корень проекта>/locales)
DEFAULT_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
#: Язык по умолчанию — из него берутся отсутствующие в переводе ключи
DEFAULT_LANG = "ru"


class Safe(str):
    """Строка, которая уже является корректным HTML и не экранируется при подстановке."""

    def __add__(self, other: str) -> "Safe":  # Safe + Safe остаётся Safe
        return Safe(str.__add__(self, other)) if isinstance(other, Safe) else str.__add__(self, other)


def join_safe(separator: str, parts: list[str]) -> Safe:
    """Склеивает готовые HTML-фрагменты (разделитель считается безопасным)."""
    return Safe(separator.join(parts))


def escape(value: Any) -> str:
    """Экранирует значение для HTML (Safe-строки не трогает)."""
    if isinstance(value, Safe):
        return value
    return html.escape(str(value), quote=False)


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """{"a": {"b": "x"}} -> {"a.b": "x"}"""
    result: dict[str, str] = {}
    for key, value in (data or {}).items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            result.update(_flatten(value, full + "."))
        elif value is not None:
            result[full] = str(value)
    return result


class _KeepMissing(dict):
    """Для str.format_map: неизвестный плейсхолдер остаётся в тексте как есть."""

    def __missing__(self, key: str) -> str:
        logger.warning("В тексте не передан параметр {%s}", key)
        return "{" + key + "}"


class Locale:
    """Набор текстов одного языка (с запасным языком по умолчанию)."""

    def __init__(self, messages: dict[str, str], lang: str = DEFAULT_LANG) -> None:
        self.messages = messages
        self.lang = lang
        self._reported: set[str] = set()

    @classmethod
    def load(cls, directory: Path = DEFAULT_LOCALES_DIR, lang: str = DEFAULT_LANG) -> "Locale":
        """
        Загружает `<directory>/<lang>.yaml` поверх `<directory>/ru.yaml`.

        :raises FileNotFoundError: если нет даже файла языка по умолчанию
        """
        directory = Path(directory)
        default_file = directory / f"{DEFAULT_LANG}.yaml"
        messages = _flatten(yaml.safe_load(default_file.read_text(encoding="utf-8")) or {})
        if lang != DEFAULT_LANG:
            lang_file = directory / f"{lang}.yaml"
            if lang_file.exists():
                messages.update(_flatten(yaml.safe_load(lang_file.read_text(encoding="utf-8")) or {}))
            else:
                logger.warning("Файл локализации %s не найден — используется %s", lang_file, DEFAULT_LANG)
        logger.info("Локализация %s загружена: %d строк", lang, len(messages))
        return cls(messages, lang)

    def lookup(self, key: str, platform: str | None = None) -> str | None:
        """Текст по ключу с учётом переопределения для платформы (или None)."""
        if platform:
            value = self.messages.get(f"{platform}.{key}")
            if value is not None:
                return value
        return self.messages.get(key)

    def get(self, key: str, platform: str | None = None) -> str:
        """Текст по ключу; для отсутствующего ключа — «[ключ]» и предупреждение в логе."""
        value = self.lookup(key, platform)
        if value is None:
            if key not in self._reported:
                self._reported.add(key)
                logger.error("Нет текста для ключа %r (язык %s)", key, self.lang)
            return f"[{key}]"
        return value


# ----------------------------------------------------------------------------- глобальная локаль
_locale: Locale | None = None


def setup_locale(directory: Path = DEFAULT_LOCALES_DIR, lang: str = DEFAULT_LANG) -> Locale:
    """Загружает локаль при старте приложения (вызывается из точки входа)."""
    global _locale
    _locale = Locale.load(directory, lang)
    return _locale


def get_locale() -> Locale:
    """Текущая локаль; если её ещё не настроили — загружается язык по умолчанию."""
    global _locale
    if _locale is None:
        _locale = Locale.load()
    return _locale


class Translator:
    """
    Доступ к текстам для одной платформы.

    :param platform: "telegram", "vk" или None (только общие тексты)
    :param locale: конкретная локаль; по умолчанию — глобальная (см. `setup_locale`)
    """

    def __init__(self, platform: str | None = None, locale: Locale | None = None) -> None:
        self.platform = platform
        self._locale = locale

    @property
    def locale(self) -> Locale:
        return self._locale or get_locale()

    def raw(self, key: str) -> str:
        """Шаблон как есть, без подстановок."""
        return self.locale.get(key, self.platform)

    def __call__(self, key: str, **params: Any) -> Safe:
        """Текст в виде безопасного HTML: значения параметров экранируются."""
        template = self.raw(key)
        if "platform" not in params and "{platform}" in template:
            params["platform"] = Safe(escape(self.raw("platform.name")))
        return Safe(template.format_map(_KeepMissing({k: escape(v) for k, v in params.items()})))

    def plain(self, key: str, **params: Any) -> str:
        """Текст без HTML-экранирования (для сообщений об ошибках, имён файлов и т.п.)."""
        template = self.raw(key)
        return template.format_map(_KeepMissing({k: str(v) for k, v in params.items()}))

    def has(self, key: str) -> bool:
        return self.locale.lookup(key, self.platform) is not None


#: Общий переводчик без привязки к платформе (почтовый слой, сервисы)
t = Translator()
