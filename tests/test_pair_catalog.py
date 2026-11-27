from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from infrastructure.db.catalog import ASSETS, PAIRS
from infrastructure.db.models import AssetModel, Base, PairModel
from infrastructure.db.pair_catalog import list_enabled_pairs


@pytest.mark.asyncio
async def test_enabled_catalogue_uses_pair_and_both_asset_flags() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AssetModel.__table__, PairModel.__table__])
    with Session(engine) as storage:
        storage.add_all(AssetModel(**row) for row in ASSETS)
        storage.add_all(PairModel(**row) for row in PAIRS)
        storage.flush()
        session = AsyncMock(spec=AsyncSession)
        session.scalars.side_effect = storage.scalars
        pairs = await list_enabled_pairs(session)
        assert len(pairs) == 12
        assert {pair.id for pair in pairs} == {row["id"] for row in PAIRS}
        disabled = storage.get(AssetModel, "RUB")
        disabled.enabled = False
        storage.flush()
        pairs = await list_enabled_pairs(session)
        assert len(pairs) == 6
        assert all("RUB" not in (p.base_code, p.quote_code) for p in pairs)
        disabled = storage.query(PairModel).filter_by(base_code="BTC", quote_code="USD").one()
        disabled.enabled = False
        storage.flush()
        pairs = await list_enabled_pairs(session)
        assert len(pairs) == 5
        assert not any((p.base_code, p.quote_code) == ("BTC", "USD") for p in pairs)
        storage.get(AssetModel, "BTC").enabled = False
        storage.flush()
        pairs = await list_enabled_pairs(session)
        assert len(pairs) == 4
        assert all(p.base_code != "BTC" for p in pairs)
        sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "pairs.enabled IS true" in sql
        assert "base_asset.enabled IS true" in sql and "quote_asset.enabled IS true" in sql
    engine.dispose()
