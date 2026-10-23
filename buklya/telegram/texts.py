"""Тексты Telegram-бота: общий файл локализации + переопределения из секции `telegram:`."""

from buklya.i18n import Translator, escape
from buklya.telegram import PLATFORM

#: t("ключ", параметр=значение) -> HTML для parse_mode=HTML
t = Translator(PLATFORM)

__all__ = ["t", "escape"]
