"""
Один пользователь в нескольких мессенджерах.

Проверяется:
    * ящик, подключённый в Telegram, после связки виден и работает во ВКонтакте;
    * новое письмо приходит в оба мессенджера, ответить можно из любого;
    * боты в разных процессах (отдельные поллеры Telegram и ВКонтакте) доставляют
      каждое письмо в каждый мессенджер ровно один раз;
    * при слиянии профилей данные перешифровываются, дубли ящиков схлопываются;
    * отвязка мессенджера и пользователь, заблокировавший бота.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, MessageEntity, Update, User

from buklya.app_context import AppContext
from buklya.config import Settings
from buklya.crypto import CredentialCipher, generate_master_key
from buklya.db.database import Database
from buklya.db.repository import Repository
from buklya.mail.models import MailCredentials, ParsedEmail
from buklya.mail.providers import ProviderPreset, ProviderRegistry
from buklya.services.linking import LinkService
from buklya.services.mail_service import PollResult
from buklya.services.notifier import RecipientUnavailable
from buklya.services.poller import MailPoller
from buklya.telegram.app import build_dispatcher
from buklya.telegram.notifier import TelegramNotifier
from buklya.vk.context import VkDeps
from buklya.vk.notifier import VkNotifier
from buklya.vk.router import VkDispatcher
from tests.test_bot_flow import FakeSession, _ids
from tests.test_vk_flow import FakeGateway, button, message

TG_ID = 1001
VK_ID = 5005
CODE_RE = re.compile(r"/link ([A-Z0-9]{4}-[A-Z0-9]{4})")


class ServerMail:
    """Почтовый «сервер»: письма добавляются в ящик, poll отдаёт те, что новее last_uid."""

    def __init__(self) -> None:
        self.inbox: list[ParsedEmail] = []
        self.sent: list[dict] = []
        self.polls = 0

    def deliver(self, uid: int, subject: str) -> None:
        self.inbox.append(ParsedEmail(uid=uid, subject=subject, from_="Босс <boss@corp.ru>", from_addr="boss@corp.ru",
                                      to="me@yandex.ru", message_id=f"<m{uid}@corp.ru>", text=f"Текст {uid}"))

    async def check_credentials(self, creds):
        return 777, max((e.uid for e in self.inbox), default=10)

    async def poll(self, creds, last_uid, uid_validity):
        self.polls += 1
        new = [e for e in self.inbox if e.uid > last_uid]
        top = max([last_uid, *(e.uid for e in self.inbox)])
        return PollResult(uid_validity=777, last_uid=top, messages=new)

    async def recent(self, creds, offset, limit):
        return [], 0, 777

    async def fetch(self, creds, uid):
        return next((e for e in self.inbox if e.uid == uid), None)

    async def send(self, creds, **kwargs):
        self.sent.append(kwargs)


class RecordingNotifier:
    """Уведомитель-заглушка: записывает, кому что доставлено."""

    def __init__(self, platform: str, unavailable: set[str] | None = None) -> None:
        self.platform = platform
        self.unavailable = unavailable or set()
        self.delivered: list[tuple[str, int]] = []

    async def send_email(self, external_id, *, account_title, email, ref_id):
        if external_id in self.unavailable:
            raise RecipientUnavailable("blocked")
        self.delivered.append((external_id, email.uid))

    async def notify_deactivated(self, external_id, acc):
        pass


def creds(email: str, password: str = "secret") -> MailCredentials:
    return MailCredentials(email=email, login=email, password=password, imap_host="imap.yandex.ru",
                           smtp_host="smtp.yandex.ru")


@pytest.fixture
async def env(tmp_path):
    settings = Settings(encryption_key=generate_master_key(), database_url="sqlite+aiosqlite://", _env_file=None)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await db.create_tables()
    repo = Repository(db, CredentialCipher(settings.encryption_key))
    yield settings, db, repo
    await db.close()


# ============================================================================ сквозной сценарий
@pytest.mark.asyncio
async def test_one_user_in_telegram_and_vk(env):
    settings, db, repo = env
    mail = ServerMail()
    providers = ProviderRegistry({"yandex": ProviderPreset(key="yandex", title="Яндекс", imap_host="imap.yandex.ru",
                                                           smtp_host="smtp.yandex.ru", domains=("yandex.ru",))})
    ctx = AppContext(settings=settings, db=db, repo=repo, mail=mail, providers=providers)

    # --- Telegram
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    tg = build_dispatcher(ctx, allowed_users=[])
    tg_user, tg_chat = User(id=TG_ID, is_bot=False, first_name="Тест"), Chat(id=TG_ID, type="private")

    async def tg_msg(text: str):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
        await tg.feed_update(bot, Update(update_id=next(_ids), message=Message(
            message_id=next(_ids), date=datetime.now(), chat=tg_chat, from_user=tg_user, text=text, entities=entities)))

    # --- ВКонтакте
    api = FakeGateway()
    vk = VkDispatcher(VkDeps(api=api, repo=repo, mail=mail, settings=settings, providers=providers))

    # 1. Ящик подключается в Telegram
    await tg_msg("/add")
    await tg_msg("me@yandex.ru")
    await tg_msg("app-password")
    assert any("подключён" in t for t in session.texts())

    # 2. Во ВКонтакте пока ящиков нет
    await vk.feed_message(message("/accounts"))
    assert "нет подключённых ящиков" in api.last_text()

    # 3. Код в Telegram -> ввод во ВКонтакте
    await tg_msg("/link")
    code = CODE_RE.search(session.texts()[-1]).group(1)
    await vk.feed_message(message(f"/link {code.lower()}"))  # регистр и дефис не важны
    assert "Мессенджеры связаны: Telegram, ВКонтакте" in api.last_text()
    tg_uid = await repo.resolve_user("telegram", TG_ID)
    assert await repo.resolve_user("vk", VK_ID) == tg_uid

    # Код одноразовый
    await vk.feed_message(message(f"/link {code}"))
    assert "Код не подошёл" in api.last_text()

    # 4. Ящик из Telegram виден во ВКонтакте
    await vk.feed_message(message("/accounts"))
    assert "me@yandex.ru" in api.sent[-1]["keyboard"]

    # 5. Новое письмо приходит в оба мессенджера
    mail.deliver(11, "Отчёт")
    poller = MailPoller(repo, mail, settings, [TelegramNotifier(bot), VkNotifier(api)])
    await poller.poll_all()
    assert mail.polls == 1, "в одном процессе ящик проверяется один раз на обе платформы"
    assert any("Новое письмо" in t and "Отчёт" in t for t in session.texts())
    assert any("Новое письмо" in m["text"] and "Отчёт" in m["text"] for m in api.sent)

    # Повторная проверка ничего не дублирует
    before_tg, before_vk = len(session.texts()), len(api.sent)
    await poller.poll_all()
    assert len(session.texts()) == before_tg and len(api.sent) == before_vk

    # 6. Ответ из ВКонтакте на письмо из ящика, подключённого в Telegram
    await vk.feed_event(button(api.find_button("↩️ Ответить")))
    await vk.feed_message(message("Принято"))
    await vk.feed_event(button(api.find_button("📤 Отправить"), cmid=api.sent[-1]["cmid"]))
    assert mail.sent and mail.sent[0]["subject"] == "Re: Отчёт"

    # 7. Отвязка ВКонтакте: ящики остаются в Telegram, во ВКонтакте — пустой профиль
    await vk.feed_message(message("/link"))
    await vk.feed_event(button(api.find_button("✂️ Отвязать")))
    assert any("Этот мессенджер отвязан" in text for text in api.texts())
    assert await repo.resolve_user("vk", VK_ID) != tg_uid
    assert len(await repo.list_accounts(tg_uid)) == 1
    assert await repo.list_accounts(await repo.resolve_user("vk", VK_ID)) == []


# ============================================================================ два процесса
@pytest.mark.asyncio
async def test_separate_processes_deliver_once(env):
    """Telegram и ВКонтакте в разных контейнерах: каждый со своим поллером над общей базой."""
    settings, _, repo = env
    user = await repo.resolve_user("telegram", TG_ID)
    vk_user = await repo.resolve_user("vk", VK_ID)
    await repo.merge_users(vk_user, user)
    await repo.add_account(user, creds("me@yandex.ru"), "yandex", last_uid=10, uid_validity=777)

    mail = ServerMail()
    tg, vk = RecordingNotifier("telegram"), RecordingNotifier("vk")
    tg_poller = MailPoller(repo, mail, settings, [tg])
    vk_poller = MailPoller(repo, mail, settings, [vk])

    mail.deliver(11, "Первое")
    await tg_poller.poll_all()  # Telegram-процесс успел раньше
    await vk_poller.poll_all()  # ВКонтакте не должен «потерять» письмо
    await tg_poller.poll_all()
    await vk_poller.poll_all()
    assert tg.delivered == [(str(TG_ID), 11)]
    assert vk.delivered == [(str(VK_ID), 11)]

    mail.deliver(12, "Второе")
    await vk_poller.poll_all()
    await tg_poller.poll_all()
    assert tg.delivered[-1] == (str(TG_ID), 12) and vk.delivered[-1] == (str(VK_ID), 12)
    assert len(tg.delivered) == len(vk.delivered) == 2


@pytest.mark.asyncio
async def test_new_messenger_does_not_get_old_mail(env):
    """Мессенджер, привязанный позже, получает только новые письма."""
    settings, _, repo = env
    user = await repo.resolve_user("telegram", TG_ID)
    await repo.add_account(user, creds("me@yandex.ru"), "yandex", last_uid=10, uid_validity=777)
    mail = ServerMail()
    tg, vk = RecordingNotifier("telegram"), RecordingNotifier("vk")
    poller = MailPoller(repo, mail, settings, [tg, vk])

    mail.deliver(11, "До связки")
    await poller.poll_all()
    assert tg.delivered == [(str(TG_ID), 11)] and vk.delivered == []

    await repo.merge_users(await repo.resolve_user("vk", VK_ID), user)
    await poller.poll_all()
    assert vk.delivered == [], "старое письмо в новый мессенджер не присылается"
    mail.deliver(12, "После связки")
    await poller.poll_all()
    assert vk.delivered == [(str(VK_ID), 12)]


# ============================================================================ слияние профилей
@pytest.mark.asyncio
async def test_merge_reencrypts_and_dedups(env):
    settings, _, repo = env
    tg_user = await repo.resolve_user("telegram", TG_ID)
    vk_user = await repo.resolve_user("vk", VK_ID)
    await repo.add_account(tg_user, creds("common@yandex.ru", "tg-pass"), "yandex")
    own = await repo.add_account(vk_user, creds("vk-only@mail.ru", "vk-pass"), "mailru")
    await repo.add_account(vk_user, creds("COMMON@yandex.ru", "vk-pass"), "yandex")
    ref_id = await repo.save_message(own, 5, 1, {"subject": "Тема"})

    linking = LinkService(repo)
    code, _ = await linking.issue_code(tg_user)
    result = await linking.redeem("vk", VK_ID, code)
    assert result.status == "ok" and result.merge.moved_accounts == 1 and result.merge.duplicate_accounts == 1

    accounts = {a.title: a for a in await repo.list_accounts(tg_user)}
    assert set(accounts) == {"common@yandex.ru", "vk-only@mail.ru"}
    # перешифровано ключом нового владельца — расшифровывается
    assert accounts["vk-only@mail.ru"].credentials.password == "vk-pass"
    assert accounts["common@yandex.ru"].credentials.password == "tg-pass"  # копия Telegram сохранена
    msg, _ = await repo.get_message(tg_user, ref_id)
    assert msg.meta["subject"] == "Тема"
    # старого профиля больше нет, оба мессенджера — один пользователь
    assert {(i.platform, i.external_id) for i in await repo.list_identities(tg_user)} == {
        ("telegram", str(TG_ID)), ("vk", str(VK_ID))}


@pytest.mark.asyncio
async def test_expired_code(env):
    settings, _, repo = env
    user = await repo.resolve_user("telegram", TG_ID)
    linking = LinkService(repo, ttl_minutes=-1)  # код уже истёк
    code, _ = await linking.issue_code(user)
    assert (await linking.redeem("vk", VK_ID, code)).status == "invalid"
    assert (await linking.redeem("vk", VK_ID, "XXXX-XXXX")).status == "invalid"


@pytest.mark.asyncio
async def test_blocked_messenger_is_skipped(env):
    """Заблокировал бота во ВКонтакте — письма идут только в Telegram, пока снова не напишет."""
    settings, _, repo = env
    user = await repo.resolve_user("telegram", TG_ID)
    await repo.merge_users(await repo.resolve_user("vk", VK_ID), user)
    await repo.add_account(user, creds("me@yandex.ru"), "yandex", last_uid=10, uid_validity=777)
    mail = ServerMail()
    tg, vk = RecordingNotifier("telegram"), RecordingNotifier("vk", unavailable={str(VK_ID)})
    poller = MailPoller(repo, mail, settings, [tg, vk])

    mail.deliver(11, "Первое")
    await poller.poll_all()
    assert [i.blocked for i in await repo.list_identities(user, "vk")] == [True]
    assert tg.delivered == [(str(TG_ID), 11)]

    # пользователь снова написал боту во ВКонтакте — доставка возобновляется
    vk.unavailable.clear()
    await repo.resolve_user("vk", VK_ID)
    mail.deliver(12, "Второе")
    await poller.poll_all()
    assert vk.delivered[-1] == (str(VK_ID), 12)
