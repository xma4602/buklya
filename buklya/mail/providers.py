"""
Определение IMAP/SMTP-серверов по адресу электронной почты.

Пресеты читаются из `providers.yaml` (путь задаётся в настройках).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ProviderPreset:
    """Параметры серверов одного провайдера."""

    key: str
    title: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"
    hint: str = ""
    save_to_sent: bool = True
    domains: tuple[str, ...] = ()
    mx: tuple[str, ...] = ()


class ProviderRegistry:
    """Набор известных провайдеров + логика автоопределения."""

    def __init__(self, presets: dict[str, ProviderPreset]) -> None:
        self.presets = presets

    # ------------------------------------------------------------------ загрузка
    @classmethod
    def from_file(cls, path: Path) -> "ProviderRegistry":
        """Загружает пресеты из YAML-файла. Если файла нет — пустой реестр."""
        presets: dict[str, ProviderPreset] = {}
        if not Path(path).exists():
            logger.warning("Файл провайдеров %s не найден — автонастройка недоступна", path)
            return cls(presets)
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for key, data in raw.items():
            presets[key] = ProviderPreset(
                key=key,
                title=data.get("title", key),
                imap_host=data["imap_host"],
                imap_port=int(data.get("imap_port", 993)),
                smtp_host=data.get("smtp_host", ""),
                smtp_port=int(data.get("smtp_port", 465)),
                smtp_security=data.get("smtp_security", "ssl"),
                hint=(data.get("hint") or "").strip(),
                save_to_sent=bool(data.get("save_to_sent", True)),
                domains=tuple(d.lower() for d in data.get("domains", []) or []),
                mx=tuple(m.lower() for m in data.get("mx", []) or []),
            )
        logger.info("Загружено пресетов провайдеров: %d", len(presets))
        return cls(presets)

    # ------------------------------------------------------------------ поиск
    def by_domain(self, domain: str) -> ProviderPreset | None:
        """Провайдер, у которого домен указан явно."""
        domain = domain.lower()
        for preset in self.presets.values():
            if domain in preset.domains:
                return preset
        return None

    def by_mx(self, mx_hosts: list[str]) -> ProviderPreset | None:
        """Провайдер, чьи MX-шаблоны совпали с MX-записями домена."""
        for host in mx_hosts:
            host = host.lower().rstrip(".")
            for preset in self.presets.values():
                if any(host == pat or host.endswith("." + pat) for pat in preset.mx):
                    return preset
        return None

    async def resolve(self, email: str) -> tuple[ProviderPreset, str]:
        """
        Определяет серверы для адреса.

        Возвращает (пресет, способ): способ = "domain" | "mx" | "guess".
        """
        domain = email.rsplit("@", 1)[-1].lower()
        direct = self.by_domain(domain)

        # Даже для известного домена сначала смотрим MX: например, ssau.ru может
        # обслуживаться Яндекс 360 — тогда правильные серверы у Яндекса.
        if direct is None or not direct.mx:
            mx_hosts = await lookup_mx(domain)
            via_mx = self.by_mx(mx_hosts)
            if via_mx is not None:
                hint = direct.hint if direct else via_mx.hint
                preset = ProviderPreset(**{**via_mx.__dict__, "key": direct.key if direct else via_mx.key,
                                            "title": direct.title if direct else via_mx.title, "hint": hint})
                return preset, "mx"

        if direct is not None:
            return direct, "domain"

        guess = ProviderPreset(
            key="custom",
            title=domain,
            imap_host=f"imap.{domain}",
            smtp_host=f"smtp.{domain}",
        )
        return guess, "guess"


async def lookup_mx(domain: str) -> list[str]:
    """MX-записи домена (по убыванию приоритета). При ошибке — пустой список."""
    try:
        import dns.asyncresolver  # dnspython

        answer = await asyncio.wait_for(dns.asyncresolver.resolve(domain, "MX"), timeout=5)
        records = sorted(answer, key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".") for r in records]
    except Exception as exc:  # noqa: BLE001
        logger.debug("MX для %s не получены: %s", domain, exc)
        return []
