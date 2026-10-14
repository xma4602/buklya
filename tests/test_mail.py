"""Разбор входящих и сборка исходящих писем."""

from email import message_from_bytes, policy
from email.message import EmailMessage

from buklya.mail.composer import build_message, parse_recipients, reply_subject, telegram_html_to_email_html
from buklya.mail.models import Attachment, MailCredentials
from buklya.mail.parser import html_to_text, parse_email
from buklya.telegram.render import split_text


def _sample() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Иван Петров <ivan@example.ru>"
    msg["To"] = "me@yandex.ru, other@x.ru"
    msg["Subject"] = "Тестовое письмо"
    msg["Message-ID"] = "<abc@example.ru>"
    msg.set_content("Привет!\nЭто текст.")
    msg.add_alternative("<p>Привет!</p><p>Это <b>текст</b>.</p>", subtype="html")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="отчёт.pdf")
    msg.add_attachment(b"x" * 2000, maintype="application", subtype="octet-stream", filename="big.bin")
    return msg.as_bytes()


def test_parse_email():
    parsed = parse_email(_sample(), uid=7, max_attachment_size=1000)
    assert parsed.subject == "Тестовое письмо"
    assert parsed.from_addr == "ivan@example.ru"
    assert "Иван Петров" in parsed.from_
    assert parsed.text.startswith("Привет!")
    assert "<b>текст</b>" in parsed.html
    assert [a.filename for a in parsed.attachments] == ["отчёт.pdf"]
    assert parsed.skipped_attachments and parsed.skipped_attachments[0].startswith("big.bin")
    assert parsed.reply_meta()["message_id"] == "<abc@example.ru>"


def test_html_only_email():
    msg = EmailMessage()
    msg["From"] = "a@b.ru"
    msg["Subject"] = "html"
    msg.set_content("<div>Строка 1<br>Строка 2</div><ul><li>пункт</li></ul><a href='https://x.ru'>ссылка</a>",
                    subtype="html")
    parsed = parse_email(msg.as_bytes(), 1)
    assert "Строка 1\nСтрока 2" in parsed.text
    assert "• пункт" in parsed.text
    assert "ссылка (https://x.ru)" in parsed.text


def test_html_to_text_strips_scripts():
    assert html_to_text("<style>x{}</style><script>alert(1)</script>Hi") == "Hi"


def test_build_reply_with_formatting_and_attachment():
    creds = MailCredentials(email="me@yandex.ru", login="me@yandex.ru", password="p", imap_host="i", smtp_host="s")
    html = telegram_html_to_email_html("<b>Жирный</b>\n<i>курсив</i>")
    msg = build_message(
        creds,
        to=parse_recipients("Иван <ivan@example.ru>"),
        subject=reply_subject("Тестовое письмо"),
        text="Жирный\nкурсив",
        html_body=html,
        attachments=[Attachment("photo.jpg", b"\xff\xd8\xff", "image/jpeg")],
        in_reply_to="<abc@example.ru>",
        references="<root@example.ru>",
    )
    parsed = message_from_bytes(bytes(msg), policy=policy.default)
    assert parsed["Subject"] == "Re: Тестовое письмо"
    assert parsed["In-Reply-To"] == "<abc@example.ru>"
    assert parsed["References"] == "<root@example.ru> <abc@example.ru>"
    body_html = parsed.get_body(("html",)).get_content()
    assert "<b>Жирный</b><br>" in body_html
    names = [p.get_filename() for p in parsed.iter_attachments()]
    assert names == ["photo.jpg"]


def test_reply_subject_not_duplicated():
    assert reply_subject("Re: Hi") == "Re: Hi"
    assert reply_subject("Hi") == "Re: Hi"


def test_parse_recipients_errors():
    assert parse_recipients("a@b.ru; c@d.ru") == ["a@b.ru", "c@d.ru"]
    try:
        parse_recipients("not-an-email")
    except ValueError:
        pass
    else:
        raise AssertionError("ожидалась ошибка")


def test_split_text_respects_limit():
    text = ("строка <тег> & ещё\n" * 500) + ("x" * 9000)
    chunks = split_text(text, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert len(chunks) > 3
