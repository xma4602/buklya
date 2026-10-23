"""
Сквозной сценарий бота ВКонтакте без сети.

API ВКонтакте подменяется фейком (`FakeGateway` записывает отправленные
сообщения), почтовый сервис — тем же фейком, что и в тесте Telegram.
Проверяются:
    * подключение ящика (попытка удалить сообщение с паролем, в БД — шифр);
    * приход нового письма в чат (с указанием ящика, вложением и кнопками);
    * ответ из нескольких сообщений с форматированием ВКонтакте и файлом;
    * просмотр последних писем и открытие письма по номеру.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from buklya.config import Settings
from buklya.crypto import CredentialCipher, generate_master_key
from buklya.db.database import Database
from buklya.db.repository import Repository
from buklya.mail.providers import ProviderPreset, ProviderRegistry
from buklya.services.poller import MailPoller
from buklya.vk.context import VkDeps
from buklya.vk.markup import VkText
from buklya.vk.notifier import VkNotifier
from buklya.vk.router import VkDispatcher
from tests.test_bot_flow import FakeMail

VK_ID = 5005
_ids = itertools.count(1)


class FakeGateway:
    """Подмена API ВКонтакте: запоминает всё, что бот отправил."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[str | None] = []
        self.deleted: list[int] = []
        self.uploads: list[tuple[str, bytes]] = []

    async def send(self, peer_id: int, message: VkText, *, keyboard=None, attachment=None) -> int:
        cmid = next(_ids)
        self.sent.append({"peer": peer_id, "text": message.text, "items": message.items,
                          "keyboard": keyboard, "attachment": attachment, "cmid": cmid})
        return cmid

    async def edit(self, peer_id: int, cmid: int, message: VkText, *, keyboard=None) -> None:
        self.edits.append({"peer": peer_id, "cmid": cmid, "text": message.text, "keyboard": keyboard})

    async def answer_event(self, event_id, user_id, peer_id, text=None) -> None:
        self.answers.append(text)

    async def delete(self, peer_id: int, cmid: int) -> bool:
        self.deleted.append(cmid)
        return False  # как во ВКонтакте: чужое сообщение в личке сообщество удалить не может

    async def upload_doc(self, peer_id: int, data: bytes, filename: str) -> str:
        self.uploads.append((filename, data))
        return f"doc-1_{len(self.uploads)}"

    async def upload_photo(self, peer_id: int, data: bytes, filename: str) -> str:
        self.uploads.append((filename, data))
        return f"photo-1_{len(self.uploads)}"

    async def download(self, url: str, max_size: int | None = None) -> bytes:
        return b"FILE-CONTENT"

    async def first_name(self, user_id: int) -> str | None:
        return "Иван"

    # ------------------------------------------------------------------ помощники теста
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent] + [e["text"] for e in self.edits]

    def last_text(self) -> str:
        return self.sent[-1]["text"]

    def find_button(self, startswith: str) -> dict:
        """payload последней кнопки с подписью, начинающейся с `startswith`."""
        for item in reversed(self.sent + self.edits):
            if not item.get("keyboard"):
                continue
            for row in json.loads(item["keyboard"])["buttons"]:
                for button in row:
                    if button["action"]["label"].startswith(startswith):
                        return button["action"]["payload"]
        raise AssertionError(f"кнопка {startswith!r} не найдена")


def message(text: str = "", *, payload: dict | None = None, attachments=None, format_data=None, from_id=VK_ID) -> dict:
    msg = {"peer_id": from_id, "from_id": from_id, "text": text, "conversation_message_id": next(_ids),
           "attachments": attachments or []}
    if payload is not None:
        msg["payload"] = json.dumps(payload)
    if format_data is not None:
        msg["format_data"] = format_data
    return {"message": msg, "client_info": {"button_actions": ["text", "callback"]}}


def button(payload: dict, cmid: int = 0) -> dict:
    return {"event_id": f"ev{next(_ids)}", "user_id": VK_ID, "peer_id": VK_ID,
            "conversation_message_id": cmid, "payload": payload}


@pytest.mark.asyncio
async def test_vk_full_flow(tmp_path):
    settings = Settings(encryption_key=generate_master_key(), database_url="sqlite+aiosqlite://", _env_file=None)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'vk.db'}")
    await db.create_tables()
    repo = Repository(db, CredentialCipher(settings.encryption_key))
    mail = FakeMail()
    providers = ProviderRegistry(
        {"yandex": ProviderPreset(key="yandex", title="Яндекс", imap_host="imap.yandex.ru", smtp_host="smtp.yandex.ru",
                                  domains=("yandex.ru",), mx=("yandex.net",), save_to_sent=False)}
    )
    api = FakeGateway()
    dp = VkDispatcher(VkDeps(api=api, repo=repo, mail=mail, settings=settings, providers=providers,
                             allowed_users=[VK_ID]))

    # --- «Начать»
    await dp.feed_message(message("Начать", payload={"command": "start"}))
    assert "Букля" in api.last_text() and "Иван" in api.last_text()
    assert json.loads(api.sent[-1]["keyboard"])["inline"] is False  # главное меню

    # --- подключение ящика (кнопка меню)
    await dp.feed_message(message("➕ Добавить ящик", payload={"cmd": "add"}))
    await dp.feed_message(message("Me@Yandex.ru"))
    assert "Яндекс" in api.last_text()
    await dp.feed_message(message("app-password-123"))
    assert api.deleted, "бот должен попытаться удалить сообщение с паролем"
    assert any("удалите его" in t for t in api.texts()), "если не удалилось — попросить удалить вручную"
    assert any("подключён" in t for t in api.texts())
    vk_user = await repo.resolve_user("vk", VK_ID)
    accounts = await repo.list_accounts(vk_user)
    assert len(accounts) == 1 and accounts[0].last_uid == 10

    # --- форматирование: жирный «Ящик … подключён» передан через format_data
    added = next(m for m in api.sent + api.edits if "подключён" in m["text"])
    assert "<b>" not in added["text"]

    # --- приход нового письма
    poller = MailPoller(repo, mail, settings, [VkNotifier(api)])
    await poller.poll_all()
    new_mail = next(m for m in api.sent if "Новое письмо" in m["text"])
    assert new_mail["peer"] == VK_ID
    assert "me@yandex.ru" in new_mail["text"] and "Отчёт за неделю" in new_mail["text"]
    assert any(item["type"] == "bold" for item in new_mail["items"])
    assert ("план.xlsx", b"xlsx") in api.uploads
    # HTML-оригинал — в zip: .html ВКонтакте не принимает
    import io, zipfile
    archive = next(data for name, data in api.uploads if name == "letter.zip")
    assert zipfile.ZipFile(io.BytesIO(archive)).read("letter.html") == "<p>Добрый день!</p>".encode()
    assert any(m["attachment"] and "план.xlsx" in m["text"] for m in api.sent)
    assert (await repo.list_accounts(vk_user))[0].last_uid == 11

    # --- ответ из нескольких сообщений
    await dp.feed_event(button(api.find_button("↩️ Ответить")))
    # «Добрый!» жирным (offset/length в UTF-16), «вложении» курсивом
    await dp.feed_message(message("Добрый! Отчёт ", format_data={"version": "1", "items": [
        {"type": "bold", "offset": 0, "length": 7}]}))
    await dp.feed_message(message("во вложении.", format_data={"version": "1", "items": [
        {"type": "italic", "offset": 3, "length": 8}]}))
    await dp.feed_message(message("Файл", attachments=[
        {"type": "doc", "doc": {"id": 1, "title": "отчёт.pdf", "ext": "pdf", "size": 100, "url": "https://vk.com/doc"}}]))
    panel = api.last_text()
    assert "3 сообщ." in panel and "отчёт.pdf" in panel
    await dp.feed_event(button(api.find_button("📤 Отправить"), cmid=api.sent[-1]["cmid"]))

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert sent["to"] == ["Начальник <boss@corp.ru>"]
    assert sent["subject"] == "Re: Отчёт за неделю"
    assert sent["in_reply_to"] == "<m1@corp.ru>"
    assert "<b>Добрый!</b>" in sent["html_body"] and "<i>вложении</i>" in sent["html_body"]
    assert sent["attachments"][0].filename == "отчёт.pdf"
    assert sent["attachments"][0].content == b"FILE-CONTENT"
    assert any("отправлено" in t for t in api.texts())

    # --- последние письма и открытие по номеру
    await dp.feed_message(message("/last"))
    page = next(e["text"] for e in reversed(api.edits) if "письма 1–1 из 1" in e["text"])
    assert "1. 🔵 Начальник" in page
    await dp.feed_event(button(api.find_button("1")))
    assert any(t.startswith("✉️ Письмо") for t in api.texts())

    # --- неизвестная/устаревшая кнопка отвечает всплывающим сообщением
    await dp.feed_event(button({"a": "cmp", "op": "send"}))
    assert api.answers[-1] == "Черновик уже закрыт"

    # --- чужой пользователь не пускается
    before = len(mail.sent)
    await dp.feed_message(message("/last", from_id=999))
    assert "нет доступа" in api.last_text()
    assert len(mail.sent) == before

    await db.close()
