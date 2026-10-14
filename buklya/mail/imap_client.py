"""
Синхронный IMAP-клиент на базе стандартного `imaplib`.

Все методы блокирующие — асинхронный код вызывает их через
`asyncio.to_thread` (см. `buklya.services.mail_service`).
Письма читаются командой BODY.PEEK[], поэтому бот НЕ помечает их прочитанными.
"""

from __future__ import annotations

import imaplib
import logging
import re
import time
from types import TracebackType

from buklya.mail.models import MailCredentials, MessageHeader
from buklya.mail.parser import parse_header_only

logger = logging.getLogger(__name__)

_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_STATUS_RE = re.compile(rb"(UIDVALIDITY|UIDNEXT|MESSAGES) (\d+)")
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


class ImapError(Exception):
    """Ошибка работы с IMAP-сервером (текст безопасен для показа пользователю)."""


class ImapClient:
    """Подключение к одному ящику. Используется как контекстный менеджер."""

    INBOX = "INBOX"

    def __init__(self, creds: MailCredentials, timeout: int = 30) -> None:
        self.creds = creds
        self.timeout = timeout
        self.conn: imaplib.IMAP4 | None = None

    # ================================================================ соединение
    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def connect(self) -> None:
        """Подключается и авторизуется на сервере."""
        c = self.creds
        try:
            if c.imap_ssl:
                self.conn = imaplib.IMAP4_SSL(c.imap_host, c.imap_port, timeout=self.timeout)
            else:
                self.conn = imaplib.IMAP4(c.imap_host, c.imap_port, timeout=self.timeout)
                if "STARTTLS" in self.conn.capabilities:
                    self.conn.starttls()
        except OSError as exc:
            raise ImapError(f"Не удалось подключиться к {c.imap_host}:{c.imap_port} ({exc})") from exc

        try:
            self._login()
        except imaplib.IMAP4.error as exc:
            self.close()
            raise ImapError(f"Сервер отклонил логин/пароль: {_text(exc)}") from exc

    def _login(self) -> None:
        """LOGIN, а для паролей с не-ASCII символами — AUTHENTICATE PLAIN."""
        assert self.conn is not None
        login, password = self.creds.login, self.creds.password
        if login.isascii() and password.isascii():
            self.conn.login(login, password)
        else:
            token = f"\0{login}\0{password}".encode("utf-8")
            self.conn.authenticate("PLAIN", lambda _: token)

    def close(self) -> None:
        """Корректно закрывает соединение (ошибки игнорируются)."""
        if self.conn is None:
            return
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    # ================================================================ helpers
    def _ok(self, result: tuple[str, list], what: str) -> list:
        """Проверяет ответ сервера, иначе бросает ImapError."""
        typ, data = result
        if typ != "OK":
            raise ImapError(f"{what}: {_text(data)}")
        return data

    def _select_inbox(self) -> None:
        assert self.conn is not None
        self._ok(self.conn.select(self.INBOX, readonly=True), "Не удалось открыть «Входящие»")

    # ================================================================ операции
    def inbox_status(self) -> tuple[int, int, int]:
        """(UIDVALIDITY, UIDNEXT, количество писем) папки «Входящие»."""
        assert self.conn is not None
        data = self._ok(self.conn.status(self.INBOX, "(UIDVALIDITY UIDNEXT MESSAGES)"), "STATUS")
        values = {k.decode(): int(v) for k, v in _STATUS_RE.findall(data[0] or b"")}
        uidnext = values.get("UIDNEXT", 0)
        if not uidnext:
            # Некоторые серверы не отдают UIDNEXT — вычисляем по максимальному UID
            self._select_inbox()
            uids = self.search_uids("ALL")
            uidnext = (max(uids) + 1) if uids else 1
        return values.get("UIDVALIDITY", 0), uidnext, values.get("MESSAGES", 0)

    def search_uids(self, criteria: str = "ALL") -> list[int]:
        """UID писем во «Входящих» по критерию IMAP SEARCH (по возрастанию)."""
        assert self.conn is not None
        data = self._ok(self.conn.uid("SEARCH", None, criteria), "SEARCH")
        return sorted(int(x) for x in (data[0] or b"").split())

    def new_uids(self, last_uid: int) -> list[int]:
        """UID писем, пришедших после `last_uid`."""
        self._select_inbox()
        # Диапазон N:* всегда возвращает хотя бы последнее письмо, поэтому фильтруем
        return [u for u in self.search_uids(f"UID {last_uid + 1}:*") if u > last_uid]

    def fetch_raw(self, uid: int) -> bytes | None:
        """Полный текст письма (RFC 822) без установки флага «прочитано»."""
        assert self.conn is not None
        self._select_inbox()
        data = self._ok(self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])"), "FETCH")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return None

    def recent_headers(self, offset: int = 0, limit: int = 10) -> tuple[list[MessageHeader], int]:
        """
        Заголовки последних писем (новые — первыми).

        :param offset: сколько самых новых писем пропустить (для постраничного просмотра)
        :return: (список заголовков, общее число писем во «Входящих»)
        """
        assert self.conn is not None
        self._select_inbox()
        uids = self.search_uids("ALL")
        total = len(uids)
        uids.reverse()
        page = uids[offset : offset + limit]
        if not page:
            return [], total

        uid_set = ",".join(str(u) for u in page)
        data = self._ok(
            self.conn.uid("FETCH", uid_set, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"),
            "FETCH",
        )
        headers: dict[int, MessageHeader] = {}
        for item in data:
            if not (isinstance(item, tuple) and len(item) >= 2):
                continue
            meta = item[0]
            uid_match = _UID_RE.search(meta)
            if not uid_match:
                continue
            flags_match = _FLAGS_RE.search(meta)
            seen = bool(flags_match and b"\\Seen" in flags_match.group(1))
            uid = int(uid_match.group(1))
            headers[uid] = parse_header_only(item[1], uid, seen)
        # Возвращаем в порядке «новые сверху»
        return [headers[u] for u in page if u in headers], total

    def mark_seen(self, uid: int) -> None:
        """Помечает письмо прочитанным."""
        assert self.conn is not None
        self._ok(self.conn.select(self.INBOX), "SELECT")
        self._ok(self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)"), "STORE")

    def find_sent_folder(self) -> str | None:
        """Имя папки «Отправленные» по флагу \\Sent (RFC 6154)."""
        assert self.conn is not None
        typ, data = self.conn.list()
        if typ != "OK":
            return None
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if m and b"\\sent" in m.group("flags").lower():
                return m.group("name").decode("utf-8", errors="replace")
        return None

    def append_to_sent(self, raw: bytes) -> bool:
        """Кладёт копию отправленного письма в «Отправленные». True — если удалось."""
        assert self.conn is not None
        folder = self.find_sent_folder()
        if not folder:
            logger.info("Папка «Отправленные» не найдена на %s", self.creds.imap_host)
            return False
        typ, _ = self.conn.append(folder, "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw)
        return typ == "OK"


def _text(value: object) -> str:
    """Приводит ответ сервера (bytes/список bytes/исключение) к строке."""
    if isinstance(value, Exception):
        value = value.args[0] if value.args else str(value)
    if isinstance(value, list):
        value = b" ".join(v for v in value if isinstance(v, bytes)) if value else b""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
