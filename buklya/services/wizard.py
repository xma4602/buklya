"""
Общая логика мастера подключения ящика (не зависит от платформы).

Мастер хранит промежуточные данные в словаре состояния диалога; здесь —
функции, которые из этого словаря собирают учётные данные, разбирают ручной
ввод серверов и т.п. Telegram и ВКонтакте используют их одинаково.
"""

from __future__ import annotations

import re
from typing import Any

from buklya.i18n import t
from buklya.mail.models import MailCredentials
from buklya.mail.providers import ProviderPreset

#: Простейшая проверка адреса почты
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Ключи текстов для способа определения серверов (ProviderRegistry.resolve)
SOURCE_KEYS = {"domain": "accounts.source_domain", "mx": "accounts.source_mx", "guess": "accounts.source_guess"}


def is_email(value: str) -> bool:
    """Похоже ли значение на адрес почты."""
    return bool(EMAIL_RE.match(value))


def preset_state(email: str, preset: ProviderPreset) -> dict[str, Any]:
    """Данные мастера после определения провайдера по адресу."""
    return {
        "email": email,
        "login": email,
        "provider": preset.key,
        "imap_host": preset.imap_host,
        "imap_port": preset.imap_port,
        "imap_ssl": True,
        "smtp_host": preset.smtp_host,
        "smtp_port": preset.smtp_port,
        "smtp_security": preset.smtp_security,
        "save_to_sent": preset.save_to_sent,
    }


def creds_from_state(data: dict[str, Any]) -> MailCredentials:
    """Собирает учётные данные из данных мастера подключения."""
    return MailCredentials(
        email=data["email"],
        login=data.get("login") or data["email"],
        password=data.get("password", ""),
        imap_host=data["imap_host"],
        imap_port=int(data.get("imap_port", 993)),
        imap_ssl=bool(data.get("imap_ssl", True)),
        smtp_host=data.get("smtp_host", ""),
        smtp_port=int(data.get("smtp_port", 465)),
        smtp_security=data.get("smtp_security", "ssl"),
        save_to_sent=bool(data.get("save_to_sent", True)),
    )


def parse_server(text: str, default_port: int) -> tuple[str, int, str | None]:
    """
    Разбирает строку `host[:port][:ssl|starttls|none]`.

    :raises ValueError: с текстом, который можно показать пользователю
    """
    parts = [p.strip() for p in text.strip().split(":") if p.strip()]
    if not parts or " " in parts[0]:
        raise ValueError(t.plain("accounts.server_format_error"))
    host = parts[0]
    try:
        port = int(parts[1]) if len(parts) > 1 else default_port
    except ValueError as exc:
        raise ValueError(t.plain("accounts.server_format_error")) from exc
    security = parts[2].lower() if len(parts) > 2 else None
    if security and security not in ("ssl", "starttls", "none"):
        raise ValueError(t.plain("accounts.server_security_error"))
    return host, port, security


def imap_state(host: str, port: int, security: str | None) -> dict[str, Any]:
    """Данные мастера после ручного ввода IMAP-сервера."""
    return {"imap_host": host, "imap_port": port, "imap_ssl": security != "none" and port != 143}


def smtp_state(host: str, port: int, security: str | None) -> dict[str, Any]:
    """Данные мастера после ручного ввода SMTP-сервера (тип защиты — по порту, если не указан)."""
    security = security or ("starttls" if port == 587 else "ssl" if port == 465 else "none")
    return {"smtp_host": host, "smtp_port": port, "smtp_security": security, "provider": "custom"}


def humanize_error(exc: Exception, limit: int = 400) -> str:
    """Текст ошибки для пользователя (без трассировок)."""
    text = str(exc) or exc.__class__.__name__
    return text[:limit]
