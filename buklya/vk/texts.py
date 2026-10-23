"""Тексты бота ВКонтакте: общий файл локализации + переопределения из секции `vk:`."""

from buklya.i18n import Safe, Translator, escape
from buklya.vk import PLATFORM

#: t("ключ", параметр=значение) -> HTML, который перед отправкой переводится
#: в формат ВКонтакте (см. buklya.vk.markup.html_to_vk)
t = Translator(PLATFORM)

__all__ = ["t", "escape", "Safe"]
