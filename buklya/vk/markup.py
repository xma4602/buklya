"""
Форматирование текста во ВКонтакте.

ВКонтакте не понимает HTML. Форматирование передаётся отдельно от текста —
параметром `format_data` (https://dev.vk.ru/ru/reference/objects/message#format_data):

    {"version": 1, "items": [{"type": "bold", "offset": 0, "length": 5},
                             {"type": "url", "offset": 6, "length": 4, "url": "https://…"}]}

Смещения и длины считаются в UTF-16 (как в JavaScript). Поддерживаются
bold, italic, underline и url.

Здесь две функции:
    * `html_to_vk`        — HTML из файла локализации -> (текст, format_data) для отправки;
    * `vk_to_html`        — сообщение пользователя (текст + format_data) -> HTML для письма.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

#: Максимальная длина сообщения ВКонтакте (с запасом от лимита 4096)
VK_TEXT_LIMIT = 4000

#: HTML-теги -> типы форматирования ВКонтакте
_TAG_TYPES = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "u": "underline", "ins": "underline"}


def utf16_len(text: str) -> int:
    """Длина строки в единицах UTF-16 (так ВКонтакте считает смещения)."""
    return len(text.encode("utf-16-le")) // 2


# ============================================================================ HTML -> ВКонтакте
@dataclass
class VkText:
    """Текст сообщения для ВКонтакте и его форматирование."""

    text: str
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def format_data(self) -> str | None:
        """JSON для параметра format_data (None — форматирования нет)."""
        if not self.items:
            return None
        return json.dumps({"version": 1, "items": self.items}, ensure_ascii=False)


class _VkHtmlParser(HTMLParser):
    """Собирает текст без тегов и диапазоны форматирования."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos16 = 0  # текущая позиция в UTF-16 (имя «offset» занято самим HTMLParser)
        self.items: list[dict[str, Any]] = []
        self.stack: list[tuple[str, int, dict[str, Any]]] = []  # (tag, start, extra)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.handle_data("\n")
            return
        if tag in _TAG_TYPES:
            self.stack.append((tag, self.pos16, {"type": _TAG_TYPES[tag]}))
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self.stack.append((tag, self.pos16, {"type": "url", "url": href}))
        # Остальные теги (code, pre, blockquote, s, span…) ВКонтакте не поддерживает —
        # их содержимое остаётся обычным текстом.

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, start, extra = self.stack.pop(index)
                if self.pos16 > start:
                    self.items.append({"offset": start, "length": self.pos16 - start, **extra})
                return

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.pos16 += utf16_len(data)


def html_to_vk(markup: str) -> VkText:
    """
    Переводит HTML-разметку (как в файле локализации) в текст + format_data.

    Неподдерживаемые теги отбрасываются, сущности (&lt; &amp;) раскрываются.
    """
    parser = _VkHtmlParser()
    parser.feed(markup or "")
    parser.close()
    items = sorted(parser.items, key=lambda item: (item["offset"], -item["length"]))
    return VkText("".join(parser.parts), items)


def truncate(vk: VkText, limit: int = VK_TEXT_LIMIT) -> VkText:
    """Обрезает текст до лимита ВКонтакте, убирая форматирование за его пределами."""
    if len(vk.text) <= limit:
        return vk
    text = vk.text[: limit - 1] + "…"
    border = utf16_len(text)
    items = []
    for item in vk.items:
        if item["offset"] >= border:
            continue
        item = dict(item)
        item["length"] = min(item["length"], border - item["offset"])
        items.append(item)
    return VkText(text, items)


def split_plain(text: str, limit: int) -> list[str]:
    """Делит длинный текст на части не длиннее `limit`, по возможности по границам строк."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна строка длиннее лимита — режем по символам
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()] or [""]


# ============================================================================ ВКонтакте -> HTML
#: Порядок вложенности тегов при сборке HTML (ссылка — снаружи)
_OPEN_ORDER = ("url", "bold", "italic", "underline")
_TAGS = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>")}


def vk_to_html(text: str, format_data: dict[str, Any] | None) -> str:
    """
    Сообщение пользователя ВКонтакте -> HTML-фрагмент (b, i, u, a) для письма.

    Переносы строк остаются символами \\n — их превращает в <br> `chat_html_to_email_html`.
    """
    items = [item for item in (format_data or {}).get("items", []) if item.get("type") in (*_TAGS, "url")]
    if not items:
        return html.escape(text or "", quote=False)

    # Позиция UTF-16 -> индекс символа в строке Python
    positions = [0]
    for char in text:
        positions.append(positions[-1] + utf16_len(char))
    to_index = {pos: index for index, pos in enumerate(positions)}

    def index_of(pos16: int) -> int:
        pos16 = max(0, min(pos16, positions[-1]))
        while pos16 not in to_index:  # попали в середину суррогатной пары
            pos16 -= 1
        return to_index[pos16]

    spans = []
    bounds = {0, len(text)}
    for item in items:
        start = index_of(int(item.get("offset", 0)))
        end = index_of(int(item.get("offset", 0)) + int(item.get("length", 0)))
        if end > start:
            spans.append((start, end, item))
            bounds.update((start, end))

    result = []
    points = sorted(bounds)
    for start, end in zip(points, points[1:]):
        piece = html.escape(text[start:end], quote=False)
        active = [item for s, e, item in spans if s <= start and end <= e]
        for kind in reversed(_OPEN_ORDER):
            for item in active:
                if item["type"] != kind:
                    continue
                if kind == "url":
                    url = str(item.get("url") or "")
                    if url.startswith(("http://", "https://")):
                        piece = f'<a href="{html.escape(url, quote=True)}">{piece}</a>'
                else:
                    opening, closing = _TAGS[kind]
                    piece = f"{opening}{piece}{closing}"
                break
        result.append(piece)
    return "".join(result)
