"""IMAP-клиент: разбор ответов сервера (без сети, на фейковом соединении)."""

from email.message import EmailMessage

from buklya.mail import imap_client
from buklya.mail.imap_client import ImapClient
from buklya.mail.models import MailCredentials


def _raw(subject: str) -> bytes:
    m = EmailMessage()
    m["From"] = "Пётр <petr@x.ru>"
    m["Subject"] = subject
    m["Date"] = "Wed, 14 Oct 2026 10:00:00 +0400"
    m.set_content("body")
    return m.as_bytes()


class FakeConn:
    """Имитирует imaplib.IMAP4_SSL с ящиком из трёх писем (UID 5, 7, 9)."""

    capabilities = ("IMAP4rev1",)

    def __init__(self, *args, **kwargs):
        self.messages = {5: _raw("Первое"), 7: _raw("Второе"), 9: _raw("Третье")}
        self.appended = []

    def login(self, user, password):
        return "OK", [b"logged in"]

    def logout(self):
        return "BYE", []

    def select(self, box, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def status(self, box, items):
        return "OK", [b'INBOX (UIDVALIDITY 42 UIDNEXT 10 MESSAGES 3)']

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            crit = args[-1]
            uids = sorted(self.messages)
            if crit.startswith("UID "):
                start = int(crit.split()[1].split(":")[0])
                uids = [u for u in uids if u >= start] or [max(self.messages)]
            return "OK", [" ".join(map(str, uids)).encode()]
        if cmd == "FETCH":
            uid_set, what = args
            data = []
            for i, uid in enumerate(int(u) for u in uid_set.split(",")):
                flags = b"\\Seen" if uid == 5 else b""
                body = self.messages[uid]
                if "HEADER.FIELDS" in what:
                    body = body.split(b"\n\n")[0] + b"\r\n\r\n"
                data.append((f"{i+1} (UID {uid} FLAGS (".encode() + flags + b") BODY[] {%d}" % len(body), body))
                data.append(b")")
            return "OK", data
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def append(self, folder, flags, date, raw):
        self.appended.append(folder)
        return "OK", [b""]


def test_imap_operations(monkeypatch):
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", FakeConn)
    creds = MailCredentials(email="a@b.ru", login="a@b.ru", password="p", imap_host="imap.b.ru")
    with ImapClient(creds) as client:
        assert client.inbox_status() == (42, 10, 3)
        assert client.new_uids(7) == [9]
        assert client.new_uids(9) == []  # «9:*» вернёт 9, но он уже обработан
        headers, total = client.recent_headers(0, 2)
        assert total == 3
        assert [h.uid for h in headers] == [9, 7]
        assert headers[0].subject == "Третье" and headers[0].from_ == "Пётр" and not headers[0].seen
        assert b"Subject" in client.fetch_raw(5)
        assert client.find_sent_folder() == '"Sent"'
        assert client.append_to_sent(b"raw")
