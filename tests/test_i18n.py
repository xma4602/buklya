"""Локализация: все ключи из кода есть в locales/ru.yaml, подстановки экранируются."""

import re
from pathlib import Path

from buklya.i18n import Locale, Safe, Translator
from buklya.vk.markup import html_to_vk, vk_to_html

ROOT = Path(__file__).resolve().parent.parent
#: t("ключ" / t.plain("ключ" / tr("ключ" — литеральные ключи в коде
KEY_RE = re.compile(r"""\b(?:t|tr)(?:\.plain|\.raw)?\(\s*["']([a-z_]+\.[a-z0-9_.]+)["']""")


def _platform_for(path: Path) -> str | None:
    parts = path.parts
    if "telegram" in parts:
        return "telegram"
    if "vk" in parts:
        return "vk"
    return None


def test_all_keys_exist():
    locale = Locale.load(ROOT / "locales", "ru")
    missing = []
    for path in (ROOT / "buklya").rglob("*.py"):
        for key in KEY_RE.findall(path.read_text(encoding="utf-8")):
            platform = _platform_for(path)
            # общий код (services) используется обеими платформами — ключ должен быть у обеих
            platforms = [platform] if platform else ["telegram", "vk"]
            for p in platforms:
                if locale.lookup(key, p) is None:
                    missing.append(f"{path.relative_to(ROOT)}: {key} ({p})")
    assert not missing, "нет текстов в ru.yaml:\n" + "\n".join(missing)


def test_platform_override_and_escaping():
    locale = Locale({"a.b": "общий <b>{x}</b>", "vk.a.b": "вк {x}", "platform.name": "?"})
    assert Translator("telegram", locale)("a.b", x="<script>") == "общий <b>&lt;script&gt;</b>"
    assert Translator("vk", locale)("a.b", x="1") == "вк 1"
    # Safe-фрагменты не экранируются повторно
    assert Translator("telegram", locale)("a.b", x=Safe("<i>ok</i>")) == "общий <b><i>ok</i></b>"
    # неизвестный ключ не роняет бота
    assert Translator(None, locale)("nope.key") == "[nope.key]"


def test_vk_markup_roundtrip():
    vk = html_to_vk("📮 <b>Ящик</b> &lt;a@b.ru&gt; <code>x</code> <a href=\"https://ya.ru\">ссылка</a>")
    assert vk.text == "📮 Ящик <a@b.ru> x ссылка"
    raw = vk.text.encode("utf-16-le")
    spans = {i["type"]: raw[i["offset"] * 2:(i["offset"] + i["length"]) * 2].decode("utf-16-le") for i in vk.items}
    assert spans == {"bold": "Ящик", "url": "ссылка"}

    html = vk_to_html("😀 жирный <тег>", {"items": [{"type": "bold", "offset": 3, "length": 6}]})
    assert html == "😀 <b>жирный</b> &lt;тег&gt;"
