"""Определение серверов по адресу."""

from pathlib import Path

import pytest

from buklya.mail import providers as prov
from buklya.mail.providers import ProviderRegistry

YAML = Path(__file__).resolve().parent.parent / "providers.yaml"


@pytest.mark.asyncio
async def test_known_domains(monkeypatch):
    async def no_mx(domain):
        return []

    monkeypatch.setattr(prov, "lookup_mx", no_mx)
    reg = ProviderRegistry.from_file(YAML)
    preset, source = await reg.resolve("a@gmail.com")
    assert preset.imap_host == "imap.gmail.com" and source == "domain"
    preset, _ = await reg.resolve("a@yandex.ru")
    assert preset.smtp_host == "smtp.yandex.ru" and preset.save_to_sent is False
    preset, source = await reg.resolve("a@unknown-domain.org")
    assert source == "guess" and preset.imap_host == "imap.unknown-domain.org"


@pytest.mark.asyncio
async def test_mx_detection(monkeypatch):
    async def yandex_mx(domain):
        return ["mx.yandex.net"]

    monkeypatch.setattr(prov, "lookup_mx", yandex_mx)
    reg = ProviderRegistry.from_file(YAML)
    preset, source = await reg.resolve("student@ssau.ru")
    assert source == "mx"
    assert preset.imap_host == "imap.yandex.ru"
    assert preset.key == "ssau"
