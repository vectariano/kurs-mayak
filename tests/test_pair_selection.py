"""KURS-23 scenarios through the real Dispatcher without Telegram network calls."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from contracts import AppError, AssetPair, DialogSession, ErrorCode
from bot import create_dispatcher
from infrastructure.db.catalog import PAIRS
from telegram_menu import MENU, STALE_BUTTON, create_menu_router, screen
from telegram_pair_selection import (
    BASE, KIND, QUOTE, SAME_ASSETS, UNSUPPORTED_ASSET,
    create_pair_selection_router,
)


@dataclass
class Dialogs:
    sessions: dict[UUID, DialogSession] = field(default_factory=dict)
    saves: int = 0
    conflict: bool = False

    async def get(self, user_id: UUID) -> DialogSession | None:
        return self.sessions.get(user_id)

    async def save(self, value: DialogSession, *, expected_version: int | None) -> DialogSession:
        current = self.sessions.get(value.user_id)
        version = current.version if current else None
        if self.conflict or version != expected_version:
            raise AppError(
                ErrorCode.CONFLICT, STALE_BUTTON, retryable=False, correlation_id=value.user_id,
            )
        saved = replace(value, version=(version or 0) + 1)
        self.sessions[value.user_id] = saved
        self.saves += 1
        return saved


class BotScenario:
    def __init__(self) -> None:
        self.dialogs = Dialogs({UUID(int=1): DialogSession(UUID(int=1), MENU, {}, 1)})
        self.pairs = [
            AssetPair(row["id"], row["base_code"], row["quote_code"], row["kind"])
            for row in PAIRS
        ]
        self.network = AsyncMock(return_value=True)
        self.bot = Bot("123456:TEST")
        self.bot.session.make_request = self.network
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_pair_selection_router(self.open_dialog, self.load_pairs))
        self.dispatcher.include_router(create_menu_router(self.open_dialog))

    @asynccontextmanager
    async def open_dialog(self, telegram_user_id: int, chat_id: int, create: bool) -> AsyncIterator:
        # Match the real adapter's owner-and-private-chat lookup.
        user_id = UUID(int=telegram_user_id)
        if telegram_user_id != chat_id:
            yield None, self.dialogs
            return
        if create and user_id not in self.dialogs.sessions:
            self.dialogs.sessions[user_id] = DialogSession(user_id, MENU, {}, 1)
        yield user_id if user_id in self.dialogs.sessions else None, self.dialogs

    async def load_pairs(self):
        return self.pairs

    @property
    def current(self) -> DialogSession:
        return self.dialogs.sessions[UUID(int=1)]

    async def click(
        self, route: str, *, version: int | None = None, actor: int = 1,
        chat_id: int | None = None, chat_type: ChatType = ChatType.PRIVATE,
    ) -> list:
        self.network.reset_mock()
        session = self.dialogs.sessions.get(UUID(int=actor))
        if version is None:
            version = session.version if session else 1
        message = Message(
            message_id=10, date=datetime.now(UTC),
            chat=Chat(id=actor if chat_id is None else chat_id, type=chat_type),
            text="Меню",
        )
        query = CallbackQuery(
            id="query", from_user=User(id=actor, is_bot=False, first_name="User"),
            chat_instance="test", message=message, data=f"{route}:{version}",
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, callback_query=query))
        return [call.args[1] for call in self.network.await_args_list]

    async def command(self, text: str) -> None:
        message = Message(
            message_id=11, date=datetime.now(UTC), chat=Chat(id=1, type=ChatType.PRIVATE),
            from_user=User(id=1, is_bot=False, first_name="User"), text=text,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=2, message=message))


@pytest.fixture
def scenario():
    return BotScenario()


def rendered(calls):
    return next(call for call in calls if isinstance(call, EditMessageText))


def choices(calls):
    return [row[0].text for row in rendered(calls).reply_markup.inline_keyboard]


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["rate", "crypto", "dyn", "alerts"])
async def test_every_section_selects_catalogue_pair(scenario, section) -> None:
    calls = await scenario.click(f"m:{section}")
    if section in ("dyn", "alerts"):
        assert scenario.current.state == KIND
        assert choices(calls) == ["Валюты", "Криптовалюта", "Назад", "Главное меню"]
        calls = await scenario.click("k:crypto")
    assert scenario.current.state == BASE
    base = "RUB" if section == "rate" else "BTC"
    assert base in choices(calls)
    calls = await scenario.click(f"b:{base}")
    assert scenario.current.state == QUOTE
    assert base not in choices(calls)
    calls = await scenario.click("q:USD")
    expected = next(p for p in scenario.pairs if p.base_code == base and p.quote_code == "USD")
    assert scenario.current.state == section
    assert scenario.current.payload["pair_id"] == str(expected.id)
    assert scenario.current.payload["kind"] == expected.kind
    assert f"{base}/USD" in rendered(calls).text


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["dyn", "alerts"])
async def test_dynamics_and_alerts_also_accept_fiat(scenario, section) -> None:
    await scenario.click(f"m:{section}")
    await scenario.click("k:fiat")
    await scenario.click("b:EUR")
    await scenario.click("q:RUB")
    assert scenario.current.payload["kind"] == "fiat"
    assert scenario.current.payload["quote_code"] == "RUB"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["b:DOGE", "b:BTC", "k:crypto"])
async def test_tampered_or_out_of_step_values_do_not_change_state(scenario, route) -> None:
    await scenario.click("m:rate")
    before = scenario.current
    calls = await scenario.click(route)
    assert scenario.current == before
    assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    expected = STALE_BUTTON if route.startswith("k:") else UNSUPPORTED_ASSET
    assert calls[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("section,kind,base,quote", [
    ("rate", None, "RUB", "RUB"),
    ("crypto", None, "BTC", "BTC"),
    ("dyn", "fiat", "USD", "USD"),
    ("alerts", "crypto", "ETH", "ETH"),
])
async def test_same_assets_are_rejected_and_selection_can_continue(scenario, section, kind, base, quote):
    await scenario.click(f"m:{section}")
    if kind:
        await scenario.click(f"k:{kind}")
    await scenario.click(f"b:{base}")
    before = scenario.current
    calls = await scenario.click(f"q:{quote}")
    assert calls[0].text == SAME_ASSETS
    assert scenario.current == before
    await scenario.click("q:EUR")
    assert scenario.current.payload["quote_code"] == "EUR"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ETH", "DOGE", "USD:extra"])
async def test_unsupported_crypto_quote_is_rejected(scenario, value) -> None:
    await scenario.click("m:crypto")
    await scenario.click("b:BTC")
    before = scenario.current
    calls = await scenario.click(f"q:{value}")
    assert calls[0].text == UNSUPPORTED_ASSET
    assert scenario.current == before


@pytest.mark.asyncio
async def test_pair_disabled_after_keyboard_display_is_revalidated(scenario) -> None:
    await scenario.click("m:crypto")
    await scenario.click("b:BTC")
    scenario.pairs = [p for p in scenario.pairs if (p.base_code, p.quote_code) != ("BTC", "USD")]
    before = scenario.current
    calls = await scenario.click("q:USD")
    assert calls[0].text == UNSUPPORTED_ASSET
    assert scenario.current == before


@pytest.mark.asyncio
async def test_back_moves_one_step_and_home_clears_the_selection(scenario) -> None:
    await scenario.click("m:dyn")
    await scenario.click("k:crypto")
    await scenario.click("b:BTC")
    await scenario.click("q:USD")
    await scenario.click("n:back")
    assert scenario.current.state == QUOTE and "pair_id" not in scenario.current.payload
    await scenario.click("n:back")
    assert scenario.current.state == BASE and "base_code" not in scenario.current.payload
    await scenario.click("n:back")
    assert scenario.current.state == KIND
    await scenario.click("n:back")
    assert scenario.current.state == MENU and scenario.current.payload == {}
    await scenario.click("m:alerts")
    await scenario.click("k:fiat")
    await scenario.click("n:home")
    assert scenario.current.state == MENU and scenario.current.payload == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["rate", "crypto"])
async def test_back_from_fixed_kind_returns_to_menu(scenario, section) -> None:
    await scenario.click(f"m:{section}")
    await scenario.click("n:back")
    assert scenario.current.state == MENU


@pytest.mark.asyncio
async def test_stale_duplicate_buttons_and_save_conflict_are_handled(scenario) -> None:
    calls = await scenario.click("m:dyn")
    button = rendered(calls).reply_markup.inline_keyboard[0][0].callback_data
    await scenario.click("k:fiat")
    before = scenario.current
    calls = await scenario.click("k:fiat", version=int(button.rsplit(":", 1)[1]))
    assert calls[0].text == STALE_BUTTON and scenario.current == before
    scenario.dialogs.conflict = True
    calls = await scenario.click("b:USD")
    assert calls[0].text == STALE_BUTTON and scenario.current == before


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,chat_id,chat_type", [
    (2, 1, ChatType.PRIVATE), (2, 2, ChatType.PRIVATE), (1, -10, ChatType.GROUP),
])
async def test_other_user_missing_session_and_group_are_rejected(scenario, actor, chat_id, chat_type):
    await scenario.click("m:rate")
    before = scenario.current
    calls = await scenario.click("b:USD", actor=actor, chat_id=chat_id, chat_type=chat_type)
    assert calls[0].text == STALE_BUTTON
    assert scenario.current == before


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/cancel"])
async def test_existing_reset_commands_clear_selection(scenario, command) -> None:
    await scenario.click("m:alerts")
    await scenario.click("k:fiat")
    await scenario.command(command)
    assert scenario.current.state == MENU and scenario.current.payload == {}


@pytest.mark.asyncio
async def test_old_saved_section_uses_the_existing_menu_navigation(scenario) -> None:
    scenario.dialogs.sessions[UUID(int=1)] = DialogSession(UUID(int=1), "dyn", {}, 1)
    calls = await scenario.click("n:back")
    assert scenario.current.state == MENU
    assert rendered(calls).text == screen(scenario.current)[0]


@pytest.mark.asyncio
async def test_empty_catalogue_and_keyboard_callback_size(scenario) -> None:
    calls = await scenario.click("m:crypto")
    for row in rendered(calls).reply_markup.inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64
    scenario.pairs = []
    await scenario.click("n:home")
    calls = await scenario.click("m:dyn")
    assert "Доступных пар нет" in rendered(calls).text
    assert choices(calls) == ["Назад", "Главное меню"]


@pytest.mark.asyncio
async def test_application_dispatcher_connects_selection_to_existing_db_adapters(
    scenario, monkeypatch,
) -> None:
    import bot as entrypoint

    @asynccontextmanager
    async def transaction():
        yield object()

    class SessionFactory:
        def __call__(self):
            return transaction()

        def begin(self):
            return transaction()

    def make_repository(session):
        repository = AsyncMock()
        repository.get_user_id.return_value = UUID(int=1)
        repository.get.side_effect = scenario.dialogs.get
        repository.save.side_effect = scenario.dialogs.save
        return repository

    monkeypatch.setattr(entrypoint, "PostgresSessionRepository", make_repository)
    catalogue = AsyncMock(return_value=scenario.pairs)
    monkeypatch.setattr(entrypoint, "list_enabled_pairs", catalogue)
    scenario.dispatcher = create_dispatcher(SessionFactory())
    await scenario.click("m:crypto")
    await scenario.click("b:ETH")
    await scenario.click("q:RUB")
    assert scenario.current.state == "crypto"
    assert scenario.current.payload["base_code"] == "ETH"
    assert scenario.current.payload["quote_code"] == "RUB"
    assert catalogue.await_count == 3
