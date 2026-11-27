from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from uuid import uuid4

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from contracts import AppError, AssetPair, DialogSession, ErrorCode, JsonValue
from telegram_menu import (
    BACK,
    HOME,
    MENU,
    SECTIONS,
    STALE_BUTTON,
    OpenDialog,
    screen,
)


type LoadPairs = Callable[[], Awaitable[Sequence[AssetPair]]]

KIND = "pair:kind"
BASE = "pair:base"
QUOTE = "pair:quote"

FIXED_KINDS = {"rate": "fiat", "crypto": "crypto"}
KIND_NAMES = {"fiat": "Валюты", "crypto": "Криптовалюта"}

SAME_ASSETS = "Нельзя выбрать одинаковые валюты. Выберите другую валюту."
UNSUPPORTED_ASSET = "Актив или пара не поддерживается. Выберите значение из списка."


def begin_selection(session: DialogSession, section: str) -> DialogSession:
    kind = FIXED_KINDS.get(section)
    payload: dict[str, JsonValue] = {"section": section}
    state = KIND

    if kind is not None:
        payload["kind"] = kind
        state = BASE

    return replace(session, state=state, payload=payload)


def selection_screen(
    session: DialogSession,
    pairs: Sequence[AssetPair],
) -> tuple[str, InlineKeyboardMarkup]:
    section = str(session.payload["section"])
    available = [
        pair for pair in pairs
        if pair.kind == session.payload.get("kind")
    ]
    buttons: list[tuple[str, str]]

    if session.state == KIND:
        title = "Выберите тип пары"
        buttons = [
            (name, f"k:{kind}")
            for kind, name in KIND_NAMES.items()
            if any(pair.kind == kind for pair in pairs)
        ]
    elif session.state == BASE:
        title = "Выберите базовый актив"
        codes = sorted({pair.base_code for pair in available})
        buttons = [(code, f"b:{code}") for code in codes]
    elif session.state == QUOTE:
        title = "Выберите котируемую валюту"
        codes = sorted({
            pair.quote_code
            for pair in available
            if pair.base_code == session.payload.get("base_code")
        })
        buttons = [(code, f"q:{code}") for code in codes]
    else:
        title = (
            f"Выбрана пара "
            f"{session.payload['base_code']}/{session.payload['quote_code']}"
        )
        buttons = []

    if not buttons and session.state in (KIND, BASE, QUOTE):
        title += "\nДоступных пар нет. Вернитесь назад или откройте главное меню."

    buttons.extend([("Назад", BACK), ("Главное меню", HOME)])

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{route}:{session.version}",
                )
            ]
            for label, route in buttons
        ]
    )

    return f"{SECTIONS[section]}\n{title}", markup


def advance_selection(
    session: DialogSession,
    route: str,
    pairs: Sequence[AssetPair],
) -> DialogSession:
    section = str(session.payload["section"])
    kind = session.payload.get("kind")
    base_code = session.payload.get("base_code")

    if route == HOME:
        return replace(session, state=MENU, payload={})

    if route == BACK:
        if session.state == KIND or (
            session.state == BASE and section in FIXED_KINDS
        ):
            return replace(session, state=MENU, payload={})

        if session.state == BASE:
            return begin_selection(session, section)

        if session.state == QUOTE:
            return replace(
                session,
                state=BASE,
                payload={"section": section, "kind": kind},
            )

        return replace(
            session,
            state=QUOTE,
            payload={
                "section": section,
                "kind": kind,
                "base_code": base_code,
            },
        )

    prefix, _, value = route.partition(":")
    available = [pair for pair in pairs if pair.kind == kind]

    if session.state == KIND and prefix == "k":
        if value in KIND_NAMES and any(pair.kind == value for pair in pairs):
            return replace(
                session,
                state=BASE,
                payload={"section": section, "kind": value},
            )

    elif session.state == BASE and prefix == "b":
        if any(pair.base_code == value for pair in available):
            return replace(
                session,
                state=QUOTE,
                payload={
                    "section": section,
                    "kind": kind,
                    "base_code": value,
                },
            )

    elif session.state == QUOTE and prefix == "q":
        if value == base_code:
            raise AppError(
                ErrorCode.INVALID_PAIR,
                SAME_ASSETS,
                retryable=False,
                correlation_id=uuid4(),
            )

        pair = next(
            (
                pair
                for pair in available
                if pair.base_code == base_code and pair.quote_code == value
            ),
            None,
        )

        if pair is not None:
            return replace(
                session,
                state=section,
                payload={
                    "section": section,
                    "kind": pair.kind,
                    "base_code": pair.base_code,
                    "quote_code": pair.quote_code,
                    "pair_id": str(pair.id),
                },
            )

    else:
        raise AppError(
            ErrorCode.CONFLICT,
            STALE_BUTTON,
            retryable=False,
            correlation_id=uuid4(),
        )

    raise AppError(
        ErrorCode.UNSUPPORTED_ASSET,
        UNSUPPORTED_ASSET,
        retryable=False,
        correlation_id=uuid4(),
    )


def create_pair_selection_router(
    open_dialog: OpenDialog,
    load_pairs: LoadPairs,
) -> Router:
    router = Router(name="pair_selection")

    @router.callback_query(
        F.data.regexp(
            r"^(?:m:(?:rate|crypto|dyn|alerts)(?::|$)|[kbq]:|n:)"
        )
    )
    async def select_pair(query: CallbackQuery) -> None:
        message = query.message

        if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
            await query.answer(STALE_BUTTON)
            return

        route, _, version = (query.data or "").rpartition(":")

        try:
            async with open_dialog(
                query.from_user.id,
                message.chat.id,
                False,
            ) as (user_id, sessions):
                current = await sessions.get(user_id) if user_id is not None else None

                choosing = current is not None and (
                    current.state in (KIND, BASE, QUOTE)
                    or current.state in SECTIONS and "pair_id" in current.payload
                )

                if route.startswith("n:") and not choosing:
                    raise SkipHandler

                if current is None or version != str(current.version):
                    await query.answer(STALE_BUTTON)
                    return

                pairs = await load_pairs()

                if (
                    current.state == MENU
                    and route.startswith("m:")
                    and route[2:] in SECTIONS
                ):
                    updated = begin_selection(current, route[2:])
                elif choosing:
                    updated = advance_selection(current, route, pairs)
                else:
                    await query.answer(STALE_BUTTON)
                    return

                saved = await sessions.save(
                    updated,
                    expected_version=current.version,
                )

        except AppError as error:
            await query.answer(error.user_message, show_alert=True)
            return

        if saved.state == MENU:
            title, markup = screen(saved)
        else:
            title, markup = selection_screen(saved, pairs)

        await query.answer()
        await message.edit_text(title, reply_markup=markup)

    return router