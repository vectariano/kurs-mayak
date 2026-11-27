from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from contracts import AssetPair, PairKind
from infrastructure.db.models import AssetModel, PairModel


async def list_enabled_pairs(session: AsyncSession) -> Sequence[AssetPair]:
    base = aliased(AssetModel, name="base_asset")
    quote = aliased(AssetModel, name="quote_asset")

    statement = (
        select(PairModel)
        .join(base, PairModel.base_code == base.code)
        .join(quote, PairModel.quote_code == quote.code)
        .where(
            PairModel.enabled.is_(True),
            base.enabled.is_(True),
            quote.enabled.is_(True),
            PairModel.kind == base.kind,
            quote.kind == "fiat",
            PairModel.base_code != PairModel.quote_code,
        )
        .order_by(PairModel.kind, PairModel.base_code, PairModel.quote_code)
    )

    rows = (await session.scalars(statement)).all()
    return [
        AssetPair(row.id, row.base_code, row.quote_code, cast(PairKind, row.kind))
        for row in rows
    ]