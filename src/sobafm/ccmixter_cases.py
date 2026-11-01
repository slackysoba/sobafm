"""The two reviewed recording identities supported by the opt-in source proof."""

from __future__ import annotations

from dataclasses import dataclass

HOST = "https://ccmixter.org"


@dataclass(frozen=True)
class MetadataRoute:
    name: str
    url: str
    recording_id: int | None = None  # Only these exact routes return info JSON.


@dataclass(frozen=True)
class ProofCase:
    case_id: str
    recording_id: int
    file_id: int
    filename: str
    expected_bytes: int
    duration: str
    bitrate: str
    candidate_digest: str
    candidate_file: str
    projection_file: str
    context_file: str
    projection_ids: tuple[int, ...]
    metadata: tuple[MetadataRoute, ...]

    @property
    def media_url(self) -> str:
        uploader = self.metadata[0].url.split("/")[-2]
        return f"{HOST}/content/{uploader}/{self.filename}"

    def route(self, name: str) -> MetadataRoute | None:
        return next((item for item in self.metadata if item.name == name), None)


# Immutable reviewed catalog digests, not credentials.
_WILD_DIGEST = (
    "d8e02a08042152c398fcab34b690b2a3129e4a38cff31839542c723535e52896"  # pragma: allowlist secret
)

_FRUITLESS_DIGEST = (
    "f7caef9aee0aed124b2004d4cade992627e20f5c45e52847d3214401665f8623"  # pragma: allowlist secret
)

WILD_WINDSTEPS = ProofCase(
    case_id="wild-windsteps-70993-129564",
    recording_id=70993,
    file_id=129564,
    filename="AntSurvila_-_WILD_WINDSTEPS.mp3",
    expected_bytes=4_693_829,
    duration="2:09",
    bitrate="VBR",
    candidate_digest=_WILD_DIGEST,
    candidate_file="ccmixter-playback-candidate.json",
    projection_file="import-ccmixter-playback-observed.json",
    context_file="import-ccmixter-playback-context.json",
    projection_ids=(70993, 70072, 25112),
    metadata=(
        MetadataRoute("mix_page", HOST + "/files/AntSurvila/70993"),
        MetadataRoute("parent_page", HOST + "/files/Dan_Mantau/70072"),
        MetadataRoute("root_page", HOST + "/files/lisadb/25112"),
        MetadataRoute("info", HOST + "/api/query?f=json&dataview=info&ids=70993&limit=1", 70993),
        MetadataRoute("terms", HOST + "/terms"),
        MetadataRoute("about", HOST + "/about"),
    ),
)

FRUITLESS = ProofCase(
    case_id="fruitless-70072-125900",
    recording_id=70072,
    file_id=125900,
    filename="Dan_Mantau_-_Fruitless.mp3",
    expected_bytes=8_232_794,
    duration="3:26",
    bitrate="CBR",
    candidate_digest=_FRUITLESS_DIGEST,
    candidate_file="ccmixter-fruitless-candidate.json",
    projection_file="import-ccmixter-fruitless-observed.json",
    context_file="import-ccmixter-fruitless-context.json",
    projection_ids=(70072, 25112),
    metadata=(
        MetadataRoute("mix_page", HOST + "/files/Dan_Mantau/70072"),
        MetadataRoute("root_page", HOST + "/files/lisadb/25112"),
        MetadataRoute("info", HOST + "/api/query?f=json&dataview=info&ids=70072&limit=1", 70072),
        MetadataRoute(
            "root_info", HOST + "/api/query?f=json&dataview=info&ids=25112&limit=1", 25112
        ),
        MetadataRoute("terms", HOST + "/terms"),
        MetadataRoute("about", HOST + "/about"),
    ),
)

CASES = (WILD_WINDSTEPS, FRUITLESS)


def proof_case(case_id: str) -> ProofCase:
    """Resolve only a named reviewed case, never a source-supplied definition."""
    for case in CASES:
        if case_id == case.case_id:
            return case
    raise ValueError("unknown_proof_case")
