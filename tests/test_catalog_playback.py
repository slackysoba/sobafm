from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from sobafm.audio_budget import AudioLedger
from sobafm.catalog import CatalogPage, CatalogQuery
from sobafm.catalog_playback import CatalogPlayback, QualifiedCatalog
from sobafm.track_deck import Envelope
from tests.doubles import FakeCatalog
from tests.test_catalog import credit, discovered
from tests.test_track_deck import Rig


async def test_notice_filter_preserves_provider_truth_and_single_cursor() -> None:
    first, second, third = discovered(id="1"), discovered(id="2"), discovered(id="3")
    client = FakeCatalog(
        CatalogPage(tracks=(first, second), received=10, exhausted=False),
        CatalogPage(tracks=(third,), received=1, exhausted=True),
    )
    query = CatalogQuery(tags=("piano",))
    catalog = QualifiedCatalog(
        client,
        {
            "2": credit(track_id="2", title="Different source identity", source_urls=(second.url,)),
            "3": credit(track_id="3", source_urls=(third.url,)),
        },
    )
    cursor = catalog.open(query)
    page = await cursor.next_page()
    assert not page.tracks  # missing and mismatched evidence both fail closed
    assert page.received == 10
    assert not page.exhausted
    page = await cursor.next_page()
    assert page.tracks == (third.with_credits(credit(track_id="3", source_urls=(third.url,))),)
    assert page.received == 1
    assert page.exhausted
    cursor.close()
    assert client.queries == [query]


async def test_search_compatibility_closes_the_original_cursor() -> None:
    client = FakeCatalog(
        (),
    )
    qualified = QualifiedCatalog(client, {})
    assert await qualified.search(CatalogQuery(tags=("piano",))) == ()
    assert len(client.queries) == 1


async def test_codec_identity_alone_never_enables_catalog(tmp_path: Path) -> None:
    fixture = Rig(tmp_path)
    owner = CatalogPlayback(SecretStr("fixture-client"))
    owner.runtime = fixture.supervisor.runtime
    assert not owner.ready
    with pytest.raises(ValueError, match="prerequisites"):
        owner.engine(1, AudioLedger(), lambda: None)
    assert owner.http is None
    owner.supervisor = fixture.supervisor
    assert not owner.ready  # no actual-source notice evidence
    owner.credits = {"42": credit()}
    assert owner.ready
    owner.runtime = owner.runtime.model_copy(update={"manifest_sha256": "c" * 64})
    assert not owner.ready
    owner.runtime = fixture.supervisor.runtime
    fixture.supervisor.envelope = Envelope()
    assert not owner.ready  # no audited whole allocation
    await owner.close()


async def test_qualified_owner_reuses_history_gate_and_exact_server_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Rig(tmp_path)
    owner = CatalogPlayback(
        SecretStr("fixture-client"),
        supervisor=fixture.supervisor,
        credits={"42": credit()},
    )
    owner.runtime = fixture.supervisor.runtime
    session = AsyncMock()
    # No real session, socket, decoder or producer is constructed.
    constructor = MagicMock(return_value=session)
    monkeypatch.setattr("sobafm.catalog_playback.aiohttp.ClientSession", constructor)
    first, second = AudioLedger(), AudioLedger()
    a = owner.engine(1, first, lambda: None)
    b = owner.engine(1, second, lambda: None)
    c = owner.engine(2, AudioLedger(), lambda: None)
    assert a.audio is first
    assert b.audio is second
    assert a.history is b.history
    assert a.history is not c.history
    constructor.assert_called_once_with(trust_env=False)
    await a.close()
    await b.close()
    await c.close()
    await owner.close()
    session.close.assert_awaited_once()
    assert owner.http is None
