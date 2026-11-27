import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from os import environ
from uuid import UUID

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.db.pair_catalog import list_enabled_pairs
from infrastructure.db.session_repository import PostgresSessionRepository
from ports import SessionRepository
from telegram_menu import create_menu_router
from telegram_pair_selection import create_pair_selection_router


def create_dispatcher(sessions: async_sessionmaker) -> Dispatcher:
    @asynccontextmanager
    async def open_dialog(
        telegram_user_id: int, chat_id: int, create: bool
    ) -> AsyncIterator[tuple[UUID | None, SessionRepository]]:
        async with sessions.begin() as transaction:
            repository = PostgresSessionRepository(transaction)
            user_id = await repository.get_user_id(telegram_user_id, chat_id, create=create)
            yield user_id, repository

    async def load_pairs():
        async with sessions() as session:
            return await list_enabled_pairs(session)

    dispatcher = Dispatcher()
    dispatcher.include_router(create_pair_selection_router(open_dialog, load_pairs))
    dispatcher.include_router(create_menu_router(open_dialog))
    return dispatcher


async def main() -> None:
    token = environ["BOT_TOKEN"]
    engine = create_async_engine(environ["DATABASE_URL"])
    try:
        dispatcher = create_dispatcher(async_sessionmaker(engine))
        async with Bot(token) as bot:
            await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
