"""
Точка входа: `python -m buklya [telegram|vk|all]`.

Собирает приложение: настройки -> логирование -> локализация -> шифрование и БД ->
почтовый сервис -> выбранные боты + фоновая проверка почты.

Какие боты запускать:
    python -m buklya              — те, что указаны в BOTS, иначе все настроенные (с токеном);
    python -m buklya telegram     — только Telegram;
    python -m buklya vk           — только ВКонтакте;
    python -m buklya all          — все платформы в одном процессе.

В Docker каждый бот работает в своём контейнере (см. docker-compose.yml); база общая,
поэтому пользователь видит одни и те же ящики во всех мессенджерах.
Список платформ — в `buklya.platforms.PLATFORMS`.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Запуск файлом (python buklya/__main__.py, «script» в PyCharm):
    # добавляем корень проекта в sys.path, чтобы работал `import buklya`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import contextlib
import logging
import signal

from pydantic import ValidationError

from buklya import __version__, platforms
from buklya.app_context import AppContext
from buklya.config import ConfigError, get_settings
from buklya.i18n import setup_locale
from buklya.logging_setup import setup_logging
from buklya.services.poller import MailPoller


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m buklya", description="Букля — почтовый бот")
    parser.add_argument(
        "bots",
        nargs="*",
        help="какие боты запустить: telegram, vk или all (по умолчанию — из BOTS или по заданным токенам)",
    )
    return parser.parse_args(argv)


def choose_platforms(requested: list[str], settings) -> list[str]:
    """
    Какие боты запускать: аргументы командной строки -> BOTS -> все настроенные.

    :raises ConfigError: если для выбранной платформы не хватает настроек или ботов нет вовсе
    """
    explicit = requested or settings.bots
    if explicit:
        errors = [f"{name}: {err}" for name in explicit if (err := platforms.config_error(name))]
        if errors:
            raise ConfigError("Не хватает настроек — " + "; ".join(errors))
        return list(explicit)
    configured = [name for name in platforms.PLATFORMS if platforms.config_error(name) is None]
    if not configured:
        raise ConfigError("Не настроена ни одна платформа (задайте TELEGRAM_TOKEN и/или VK_TOKEN)")
    return configured


def log_prefix(names: list[str]) -> str:
    """Префикс лог-файла: «buklya-vk_», «buklya-telegram-vk_» — у каждого контейнера свои файлы."""
    return "buklya-" + "-".join(names) + "_"


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM (Ctrl+C, docker stop) -> корректная остановка."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows: нет add_signal_handler
            loop.add_signal_handler(sig, stop.set)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = get_settings()
        requested = platforms.parse_platforms(args.bots)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    except ValueError as exc:  # неизвестное имя платформы в аргументах
        raise ConfigError(str(exc)) from exc
    names = choose_platforms(requested, settings)

    logger = setup_logging(
        settings.log_dir, settings.log_level, settings.log_retention_days, log_prefix(names), settings.log_color
    )
    logger.info("Запуск Букли v%s: %s", __version__, ", ".join(names))
    setup_locale(settings.locales_dir, settings.locale)

    # --- инфраструктура, общая для всех ботов
    ctx = await AppContext.create(settings)
    apps: list[platforms.BotApp] = []
    tasks: list[asyncio.Task] = []
    poller: MailPoller | None = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        for name in names:
            app = platforms.load_app(name).create_app(ctx)
            apps.append(app)
            await app.start()

        # --- фоновая проверка почты: только для пользователей запущенных ботов
        poller = MailPoller(ctx.repo, ctx.mail, settings, [app.notifier for app in apps])
        tasks.append(asyncio.create_task(poller.run(), name="mail-poller"))
        for app in apps:
            tasks.append(asyncio.create_task(app.run(), name=f"bot-{app.platform}"))

        # Работаем, пока не придёт сигнал остановки или не упадёт одна из задач
        stopper = asyncio.create_task(stop.wait(), name="stop-signal")
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        for task in done:
            if task is not stopper and not task.cancelled() and task.exception():
                logger.error("Задача %s завершилась с ошибкой", task.get_name(), exc_info=task.exception())
                raise task.exception()  # type: ignore[misc]
    finally:
        logger.info("Остановка…")
        if poller is not None:
            poller.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for app in apps:
            with contextlib.suppress(Exception):
                await app.close()
        await ctx.close()
        logger.info("Букля остановлена")


def run() -> None:
    """Синхронная обёртка для запуска."""
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except ConfigError as exc:  # ошибки настройки (нет токена, нет прав и т.п.) — без трассировки
        logging.getLogger("buklya").error("Ошибка настройки: %s", exc)
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        logging.getLogger("buklya").exception("Критическая ошибка, приложение остановлено")
        sys.exit(1)


if __name__ == "__main__":
    run()
