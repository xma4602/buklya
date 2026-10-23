"""
Отправка писем через SMTP (стандартный `smtplib`, блокирующий — вызывается в потоке).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from buklya.i18n import t
from buklya.mail.models import MailCredentials

logger = logging.getLogger(__name__)


class SmtpError(Exception):
    """Ошибка отправки (текст безопасен для показа пользователю)."""


def _open(creds: MailCredentials, timeout: int) -> smtplib.SMTP:
    """Открывает соединение с учётом типа защиты и авторизуется."""
    context = ssl.create_default_context()
    try:
        if creds.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(creds.smtp_host, creds.smtp_port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(creds.smtp_host, creds.smtp_port, timeout=timeout)
            server.ehlo()
            if creds.smtp_security == "starttls":
                server.starttls(context=context)
                server.ehlo()
    except (OSError, smtplib.SMTPException) as exc:
        raise SmtpError(t.plain("mail.smtp_connect_failed", host=creds.smtp_host, port=creds.smtp_port, error=exc)) from exc

    try:
        server.login(creds.login, creds.password)
    except smtplib.SMTPAuthenticationError as exc:
        server.close()
        raise SmtpError(t.plain("mail.smtp_auth_failed")) from exc
    except smtplib.SMTPException as exc:
        server.close()
        raise SmtpError(t.plain("mail.smtp_auth_error", error=exc)) from exc
    return server


def check_smtp(creds: MailCredentials, timeout: int = 30) -> None:
    """Проверяет, что с учётными данными можно авторизоваться на SMTP."""
    server = _open(creds, timeout)
    try:
        server.quit()
    except smtplib.SMTPException:
        pass


def send_message(creds: MailCredentials, message: EmailMessage, timeout: int = 30) -> None:
    """Отправляет готовое письмо."""
    server = _open(creds, timeout)
    try:
        refused = server.send_message(message)
        if refused:
            raise SmtpError(t.plain("mail.smtp_refused", recipients=", ".join(refused)))
    except smtplib.SMTPRecipientsRefused as exc:
        raise SmtpError(t.plain("mail.smtp_refused_all", recipients=", ".join(exc.recipients))) from exc
    except smtplib.SMTPException as exc:
        raise SmtpError(t.plain("mail.smtp_send_failed", error=exc)) from exc
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass
    logger.info("Письмо отправлено через %s", creds.smtp_host)
