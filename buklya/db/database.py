"""
Подключение к базе данных.

Используется асинхронный движок SQLAlchemy. Тип СУБД определяется строкой
`DATABASE_URL`:
    * sqlite+aiosqlite:///data/buklya.db        — по умолчанию;
    * postgresql+asyncpg://user:pass@host/db    — PostgreSQL (нужен пакет asyncpg).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from buklya.db.models import Base

logger = logging.getLogger(__name__)


class Database:
    """Обёртка над движком и фабрикой сессий."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        parsed = make_url(url)
        self.is_sqlite = parsed.get_backend_name() == "sqlite"

        if self.is_sqlite and parsed.database and parsed.database != ":memory:":
            # Для SQLite заранее создаём каталог под файл базы
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=not self.is_sqlite)

        if self.is_sqlite:
            # В SQLite внешние ключи (и каскадное удаление) по умолчанию выключены
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_tables(self) -> None:
        """
        Создаёт таблицы, если их нет.

        Для промышленной эксплуатации на Postgres рекомендуется подключить
        Alembic-миграции; модели для этого уже готовы (Base.metadata).
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("База данных готова (%s)", make_url(self.url).render_as_string(hide_password=True))

    async def close(self) -> None:
        """Закрывает пул соединений."""
        await self.engine.dispose()
