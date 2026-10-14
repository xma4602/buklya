"""Репозиторий: данные в БД зашифрованы, владелец проверяется."""


import pytest

from buklya.crypto import CredentialCipher, generate_master_key
from buklya.db.database import Database
from buklya.db.repository import DuplicateAccountError, Repository
from buklya.mail.models import MailCredentials


@pytest.mark.asyncio
async def test_accounts_are_encrypted(tmp_path):
    db_file = tmp_path / "t.db"
    db = Database(f"sqlite+aiosqlite:///{db_file}")
    await db.create_tables()
    repo = Repository(db, CredentialCipher(generate_master_key()))

    creds = MailCredentials(email="user@yandex.ru", login="user@yandex.ru", password="SuperSecret123",
                            imap_host="imap.yandex.ru", smtp_host="smtp.yandex.ru")
    acc = await repo.add_account(42, creds, "yandex", last_uid=10, uid_validity=1)
    assert acc.title == "user@yandex.ru"

    with pytest.raises(DuplicateAccountError):
        await repo.add_account(42, creds, "yandex")

    # Чужой пользователь не видит ящик
    assert await repo.get_account(43, acc.id) is None
    got = await repo.get_account(42, acc.id)
    assert got.credentials.password == "SuperSecret123"

    ref_id = await repo.save_message(got, 11, 1, {"subject": "Секретная тема", "from": "boss@corp.ru"})
    assert (await repo.get_message(43, ref_id)) is None
    msg, _ = await repo.get_message(42, ref_id)
    assert msg.meta["subject"] == "Секретная тема"

    await db.close()

    # В файле БД нет открытых логинов, паролей и метаданных писем
    raw = db_file.read_bytes()
    for wal in tmp_path.glob("t.db-wal"):
        raw += wal.read_bytes()
    for secret in (b"SuperSecret123", b"user@yandex.ru", b"imap.yandex.ru", "Секретная".encode(), b"boss@corp.ru"):
        assert secret not in raw

    # Поллинг-состояние и деактивация после ошибок
    db = Database(f"sqlite+aiosqlite:///{db_file}")
    repo2 = Repository(db, repo.cipher)
    for _ in range(2):
        deactivated = await repo2.save_poll_state(acc.id, error="auth failed", max_errors=2)
    assert deactivated
    assert await repo2.accounts_for_polling() == []
    assert await repo2.delete_account(42, acc.id)
    await db.close()
