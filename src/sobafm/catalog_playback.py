"""Bot-owned catalog composition. Missing qualifications never create a ready factory."""

from collections.abc import Callable, Mapping

import aiohttp

from sobafm.audio_budget import AudioLedger
from sobafm.catalog import (
    Catalog,
    CatalogCycle,
    CatalogPage,
    CatalogQuery,
    CatalogTrack,
    CreditRecord,
)
from sobafm.catalog_engine import CatalogEngine, History
from sobafm.config import ApiKey
from sobafm.jamendo import JamendoCatalog, MetadataGate
from sobafm.native import VerifiedNativeRuntime
from sobafm.track_deck import DecoderSupervisor, TrackDeck


class QualifiedCycle:
    def __init__(self, cursor: CatalogCycle, credits: Mapping[str, CreditRecord]) -> None:
        self.cursor, self.credits = cursor, credits

    def close(self) -> None:
        self.cursor.close()

    async def next_page(self) -> CatalogPage:
        page = await self.cursor.next_page()
        tracks: list[CatalogTrack] = []
        for track in page.tracks:
            record = self.credits.get(track.id)
            if record is None:
                continue  # absent discovery fields are never synthesized into notice evidence
            try:
                tracks.append(track.with_credits(record))
            except ValueError:
                continue  # changed source identity/unsafe rendering is ineligible, not no-notice
        return CatalogPage(tracks=tuple(tracks), received=page.received, exhausted=page.exhausted)


class QualifiedCatalog:
    def __init__(self, client: Catalog, credits: Mapping[str, CreditRecord]) -> None:
        self.client, self.credits = client, credits

    def open(self, query: CatalogQuery) -> QualifiedCycle:
        return QualifiedCycle(self.client.open(query), self.credits)

    async def search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        cursor = self.open(query)
        try:
            while True:
                page = await cursor.next_page()
                if page.tracks or page.exhausted:
                    return page.tracks
        finally:
            cursor.close()


class CatalogPlayback:
    """An internal qualified owner, not an operator attestation or readiness toggle.

    Production supplies neither a qualified envelope/containment supervisor nor
    an actual-source notice collection yet (#175/#130). Empty defaults therefore
    remain unavailable even when the native codec check passes. Qualified callers
    must bind that supervisor to the exact startup-verified runtime and supply
    independently established immutable records; a digest alone proves no notice coverage.
    """

    def __init__(
        self,
        client_id: ApiKey,
        *,
        supervisor: DecoderSupervisor | None = None,
        credits: Mapping[str, CreditRecord] | None = None,
    ) -> None:
        self.client_id = client_id
        self.supervisor, self.credits = supervisor, credits
        self.runtime: VerifiedNativeRuntime | None = None
        self.histories: dict[int, History] = {}
        self.gate = MetadataGate()
        self.http: aiohttp.ClientSession | None = None

    @property
    def ready(self) -> bool:
        return (
            self.runtime is not None
            and self.supervisor is not None
            and self.supervisor.runtime == self.runtime
            and self.supervisor.envelope.allocation is not None
            and bool(self.credits)
        )

    def engine(self, guild: int, audio: AudioLedger, wake: Callable[[], None]) -> CatalogEngine:
        supervisor, credits = self.supervisor, self.credits
        if not self.ready or supervisor is None or credits is None:
            raise ValueError("catalog prerequisites are unavailable")
        if self.http is None:
            self.http = aiohttp.ClientSession(trust_env=False)
        catalog = QualifiedCatalog(JamendoCatalog(self.client_id, self.http, self.gate), credits)
        history = self.histories.setdefault(guild, History())
        return CatalogEngine(
            catalog,
            lambda track, program: TrackDeck(track, program, audio, supervisor),
            audio=audio,
            history=history,
            wake=wake,
        )

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()
            self.http = None
