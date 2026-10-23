"""
Сквозные сценарии бота без сети.

Telegram Bot API подменяется фейковой сессией (запросы записываются, ответы
генерируются), почтовый сервис — фейком. Проверяются:
    * подключение ящика (пароль удаляется из чата, в БД — шифр);
    * приход нового письма в чат (с указанием ящика и вложением);
    * ответ на письмо из нескольких сообщений с форматированием и файлом;
    * просмотр последних писем и открытие письма по кнопке.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    Message,
    MessageEntity,
    Update,
    User,
)

from buklya.app_context import AppContext
from buklya.config import Settings
from buklya.crypto import CredentialCipher, generate_master_key
from buklya.db.database import Database
from buklya.db.repository import Repository
from buklya.mail.models import Attachment, MessageHeader, ParsedEmail
from buklya.mail.providers import ProviderPreset, ProviderRegistry
from buklya.services.mail_service import PollResult
from buklya.services.poller import MailPoller
from buklya.telegram.app import build_dispatcher
from buklya.telegram.notifier import TelegramNotifier

USER_ID = 1001
_ids = itertools.count(100)


# ============================================================================ фейки
class FakeSession(BaseSession):
    """Записывает все запросы к Bot API и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def close(self) -> None:  # noqa: D401
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: Any = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="documents/file.bin")
        # Методы, возвращающие Message (send_*, edit_*), получают сообщение,
        # привязанное к боту (как это делает настоящая сессия)
        if "Message" in str(method.__returning__):
            return Message(
                message_id=next(_ids),
                date=datetime.now(),
                chat=Chat(id=getattr(method, "chat_id", None) or USER_ID, type="private"),
                text=getattr(method, "text", None),
            ).as_(bot)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b"FILE-CONTENT"

    def texts(self) -> list[str]:
        return [getattr(r, "text", None) or getattr(r, "caption", None) or "" for r in self.requests]

    def of(self, name: str) -> list[TelegramMethod]:
        return [r for r in self.requests if type(r).__name__ == name]


class FakeMail:
    """Подмена MailService."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.email = ParsedEmail(
            uid=11,
            subject="Отчёт за неделю",
            from_="Начальник <boss@corp.ru>",
            from_addr="boss@corp.ru",
            to="me@yandex.ru, colleague@corp.ru",
            message_id="<m1@corp.ru>",
            text="Добрый день!\nПришлите, пожалуйста, отчёт.",
            html="<p>Добрый день!</p>",
            attachments=[Attachment("план.xlsx", b"xlsx", "application/vnd.ms-excel")],
        )

    async def check_credentials(self, creds):
        assert creds.password == "app-password-123"
        return 777, 10

    async def poll(self, creds, last_uid, uid_validity):
        assert last_uid == 10
        return PollResult(uid_validity=777, last_uid=11, messages=[self.email])

    async def recent(self, creds, offset, limit):
        return [MessageHeader(uid=11, subject="Отчёт за неделю", from_="Начальник", date="", seen=False)], 1, 777

    async def fetch(self, creds, uid):
        return self.email

    async def mark_seen(self, creds, uid):
        pass

    async def send(self, creds, **kwargs):
        self.sent.append(kwargs)


# ============================================================================ helpers
USER = User(id=USER_ID, is_bot=False, first_name="Тест")
CHAT = Chat(id=USER_ID, type="private")


def msg(text: str | None = None, **kwargs) -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(message_id=next(_ids), date=datetime.now(), chat=CHAT, from_user=USER, text=text, **kwargs),
    )


def cb(data: str) -> Update:
    message = Message(message_id=next(_ids), date=datetime.now(), chat=CHAT, text="x")
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(id=str(next(_ids)), from_user=USER, chat_instance="ci", data=data, message=message),
    )


def last_markup(session: FakeSession):
    for r in reversed(session.requests):
        markup = getattr(r, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            return markup
    raise AssertionError("нет inline-клавиатуры")


def find_button(session: FakeSession, startswith: str) -> str:
    for r in reversed(session.requests):
        markup = getattr(r, "reply_markup", None)
        if markup is None or not hasattr(markup, "inline_keyboard"):
            continue
        for row in markup.inline_keyboard:
            for b in row:
                if b.text.startswith(startswith):
                    return b.callback_data
    raise AssertionError(f"кнопка {startswith!r} не найдена")


# ============================================================================ тест
@pytest.mark.asyncio
async def test_full_flow(tmp_path):
    settings = Settings(encryption_key=generate_master_key(), database_url="sqlite+aiosqlite://", _env_file=None)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    await db.create_tables()
    repo = Repository(db, CredentialCipher(settings.encryption_key))
    mail = FakeMail()
    providers = ProviderRegistry(
        {"yandex": ProviderPreset(key="yandex", title="Яндекс", imap_host="imap.yandex.ru", smtp_host="smtp.yandex.ru",
                                  domains=("yandex.ru",), mx=("yandex.net",), save_to_sent=False)}
    )

    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    ctx = AppContext(settings=settings, db=db, repo=repo, mail=mail, providers=providers)
    dp = build_dispatcher(ctx, allowed_users=[USER_ID])

    async def feed(update: Update):
        await dp.feed_update(bot, update)

    # --- /start
    await feed(msg("/start", entities=[MessageEntity(type="bot_command", offset=0, length=6)]))
    assert "Букля" in session.texts()[-1]

    # --- подключение ящика
    await feed(msg("➕ Добавить ящик"))
    await feed(msg("Me@Yandex.ru"))
    assert "Яндекс" in session.texts()[-1]
    await feed(msg("app-password-123"))
    assert session.of("DeleteMessage"), "сообщение с паролем должно удаляться"
    assert any("подключён" in t for t in session.texts())
    user_id = await repo.resolve_user("telegram", USER_ID)
    accounts = await repo.list_accounts(user_id)
    assert len(accounts) == 1 and accounts[0].last_uid == 10

    # --- приход нового письма
    poller = MailPoller(repo, mail, settings, [TelegramNotifier(bot)])
    await poller.poll_all()
    new_mail_text = next(t for t in session.texts() if "Новое письмо" in t)
    assert "me@yandex.ru" in new_mail_text and "Отчёт за неделю" in new_mail_text
    assert any(getattr(r, "caption", "") and "план.xlsx" in r.caption for r in session.of("SendDocument"))
    assert (await repo.list_accounts(user_id))[0].last_uid == 11

    # --- ответ из нескольких сообщений
    await feed(cb(find_button(session, "↩️ Ответить")))
    await feed(msg("Добрый! Отчёт ", entities=[MessageEntity(type="bold", offset=0, length=7)]))
    await feed(msg("во вложении.", entities=[MessageEntity(type="italic", offset=3, length=8)]))
    await feed(Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=CHAT, from_user=USER,
        document=Document(file_id="F1", file_unique_id="U1", file_name="отчёт.pdf", mime_type="application/pdf", file_size=100),
        caption="Файл", )))
    panel = session.texts()[-1]
    assert "3 сообщ." in panel and "отчёт.pdf" in panel
    await feed(cb(find_button(session, "📤 Отправить")))

    assert len(mail.sent) == 1
    sent = mail.sent[0]
    assert sent["to"] == ["Начальник <boss@corp.ru>"]
    assert sent["subject"] == "Re: Отчёт за неделю"
    assert sent["in_reply_to"] == "<m1@corp.ru>"
    assert "<b>Добрый!</b>" in sent["html_body"] and "<i>вложении</i>" in sent["html_body"]
    assert sent["attachments"][0].filename == "отчёт.pdf"
    assert sent["attachments"][0].content == b"FILE-CONTENT"
    assert any("отправлено" in t for t in session.texts())

    # --- последние письма и открытие
    await feed(msg("📬 Последние письма"))
    assert any("письма 1–1 из 1" in t for t in session.texts())
    await feed(cb(find_button(session, "🔵 Начальник")))
    assert any(t.startswith("✉️ Письмо") for t in session.texts())

    # --- чужой пользователь не пускается
    stranger = User(id=999, is_bot=False, first_name="X")
    before = len(mail.sent)
    await feed(Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=999, type="private"), from_user=stranger, text="/last",
        entities=[MessageEntity(type="bot_command", offset=0, length=5)])))
    assert "нет доступа" in session.texts()[-1]
    assert len(mail.sent) == before

    await db.close()
