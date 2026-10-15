"""Validated catalog discovery and source-bound credits, without playback or provider I/O."""

import ipaddress
import re
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, model_validator

from sobafm.plan import one_line

MAX_CREDIT_BYTES = 32 * 1024
MAX_CREDIT_UNITS = 16_000
PAGE_UNITS = 1900
MAX_CREDIT_PAGES = 10
MARKUP = re.compile(r"[\\*_~|`<\[\]:.\u3002\uff0e\uff61]")


def _text(value: str) -> str:
    value = one_line(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid catalog text")
    return value


type CatalogText = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_text)]
type TrackId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
type Tag = Annotated[str, Field(pattern=r"^[a-z][a-z0-9 -]{0,39}$")]


class Model(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class CatalogQuery(Model):
    """Descriptive search input; interpretation and program titles belong to #126."""

    tags: tuple[Tag, ...] = Field(min_length=1, max_length=6)
    search: Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9 ',-]{0,119}$")] | None = None
    speed: Literal["verylow", "low", "medium", "high", "veryhigh"] | None = None
    vocalinstrumental: Literal["vocal", "instrumental"] | None = None
    acousticelectric: Literal["acoustic", "electric"] | None = None

    @model_validator(mode="after")
    def unique_tags(self) -> Self:
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("duplicate search tags")
        return self


class License(StrEnum):
    """The six accepted 3.0 Unported identities, never a generic CC flag."""

    BY = "by"
    BY_NC = "by-nc"
    BY_ND = "by-nd"
    BY_NC_ND = "by-nc-nd"
    BY_SA = "by-sa"
    BY_NC_SA = "by-nc-sa"

    @property
    def url(self) -> str:
        return f"https://creativecommons.org/licenses/{self.value}/3.0/"

    @property
    def label(self) -> str:
        return f"CC {self.value.upper()} 3.0 Unported"

    @classmethod
    def from_url(cls, value: str) -> License:
        match = re.fullmatch(
            r"https?://(?:www\.)?creativecommons\.org/licenses/"
            r"(by|by-nc|by-nd|by-nc-nd|by-sa|by-nc-sa)/3\.0/?",
            value,
        )
        if match is None:
            raise ValueError("unsupported license identity")
        return cls(match[1])


def safe_link(value: str) -> str:
    """Validate a display-only licensor/source link; never resolve or fetch it here."""
    if (
        len(value) > 1000
        or not value.isascii()
        or re.search(r"[\s\\<>\[\]()\x00-\x20\x7f]", value)
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", value, re.IGNORECASE)
    ):
        raise ValueError("unsafe credit link")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError("unsafe credit link") from None
    if (
        parsed.scheme not in ("https", "http")
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80 if parsed.scheme == "http" else 443)
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
        or "." not in host
        or host.endswith(
            (
                ".localhost",
                ".local",
                ".localdomain",
                ".internal",
                ".test",
                ".invalid",
                ".home",
                ".lan",
                ".intranet",
                ".corp",
            )
        )
        or any(
            not label or label.startswith("-") or label.endswith("-") for label in host.split(".")
        )
    ):
        raise ValueError("unsafe credit link")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", host.rsplit(".", 1)[-1]):
            raise ValueError("unsafe credit link") from None
    else:
        if not address.is_global:
            raise ValueError("unsafe credit link")
    return value


type CreditLink = Annotated[str, AfterValidator(safe_link)]


def stream_url_allowed(value: str) -> bool:
    """Validate every audio URL/redirect before any future credential-bearing request."""
    if len(value) > 4096 or not value.isascii() or re.search(r"[\s\\\x00-\x20\x7f]", value):
        return False
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", value, re.IGNORECASE):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and re.fullmatch(r"[a-z0-9.-]+", host) is not None
            and all(
                label and not label.startswith("-") and not label.endswith("-")
                for label in host.split(".")
            )
            and (host == "storage.jamendo.com" or host.endswith(".storage.jamendo.com"))
        )
    except ValueError:
        return False


def _audio(value: SecretStr) -> SecretStr:
    if not stream_url_allowed(value.get_secret_value()):
        raise ValueError("unsafe audio URL")
    return value


class Notice(Model):
    kind: Literal["copyright", "license", "disclaimer", "attribution", "modification"]
    text: Annotated[str, Field(min_length=1, max_length=MAX_CREDIT_BYTES)]
    links: tuple[CreditLink, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def printable(self) -> Self:
        if not one_line(self.text) or any(
            (ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in self.text
        ):
            raise ValueError("invalid notice text")
        return self


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _escape(value: str) -> str:
    return MARKUP.sub(r"\\\g<0>", one_line(value))


class CreditRecord(Model):
    """Complete, independently qualified source evidence, never an operator attestation.

    The ingestion owner must establish coverage of actual required notices. A digest binds that
    evidence; neither a digest nor discovery metadata proves coverage. No default record is made.
    """

    track_id: TrackId
    title: CatalogText
    artist: CatalogText
    license: License
    attribution_parties: tuple[CatalogText, ...] = Field(min_length=1, max_length=64)
    notices: tuple[Notice, ...] = Field(max_length=64)
    source_urls: tuple[CreditLink, ...] = Field(min_length=1, max_length=32)
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    def blocks(self) -> tuple[str, ...]:
        """Complete escaped credit blocks, with application-built usable links."""
        return (
            f"Song: {_escape(self.title)}",
            f"Artist: {_escape(self.artist)}",
            *(f"Credit: {_escape(party)}" for party in self.attribution_parties),
            f"License: [{self.license.label}](<{self.license.url}>)",
            "Music provided by [Jamendo](<https://www.jamendo.com/>)",
            f"Track: [Jamendo track](<https://www.jamendo.com/track/{self.track_id}>)",
            *(f"Source: [Source](<{url}>)" for url in self.source_urls),
            *(
                block
                for notice in self.notices
                for block in (
                    f"{notice.kind.capitalize()}: {_escape(notice.text)}",
                    *(f"Licensor: [Link](<{url}>)" for url in notice.links),
                )
            ),
        )

    @model_validator(mode="after")
    def complete_bounds(self) -> Self:
        if f"https://www.jamendo.com/track/{self.track_id}" not in self.source_urls:
            raise ValueError("credit sources must include the exact track")
        if len(self.model_dump_json().encode("utf-8")) > MAX_CREDIT_BYTES:
            raise ValueError("credit record exceeds raw bound")
        blocks = self.blocks()
        if _units("\n".join(blocks)) > MAX_CREDIT_UNITS:
            raise ValueError("credit record exceeds rendering bound")
        # Reserve space for the caller-bound view's song/snapshot/page labels. Text can wrap across
        # pages; links are whole blocks and must never be split by the eventual view renderer.
        pages, used = 1, 0
        for block in blocks:
            remaining = _units(block) + 1
            while remaining:
                available = PAGE_UNITS - 200 - used
                if available == 0:
                    pages, used = pages + 1, 0
                    continue
                take = min(remaining, available)
                if "](<" in block and take < remaining:
                    pages, used = pages + 1, 0
                    continue
                used += take
                remaining -= take
            if pages > MAX_CREDIT_PAGES:
                raise ValueError("credit record exceeds page bound")
        return self


class CatalogTrack(Model):
    """Discovery is not playback eligibility: only qualified, matching credits enable it."""

    id: TrackId
    title: CatalogText
    artist: CatalogText
    duration: int = Field(gt=0, le=900)
    license: License
    audio: Annotated[SecretStr, AfterValidator(_audio)] = Field(repr=False, exclude=True)
    tags: tuple[CatalogText, ...] = Field(default=(), max_length=300)
    credits: CreditRecord | None = None

    @property
    def url(self) -> str:
        return f"https://www.jamendo.com/track/{self.id}"

    @property
    def eligible(self) -> bool:
        return self.credits is not None

    def with_credits(self, record: CreditRecord) -> CatalogTrack:
        """Bind independently qualified evidence, revalidating every immutable nested field."""
        return CatalogTrack.model_validate(self.model_copy(update={"credits": record}))

    @model_validator(mode="after")
    def matching_credits(self) -> Self:
        credit = self.credits
        if credit is not None and (
            credit.track_id,
            credit.title,
            credit.artist,
            credit.license,
        ) != (self.id, self.title, self.artist, self.license):
            raise ValueError("credits conflict with discovery identity")
        return self


class Catalog(Protocol):
    async def search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        """Return bounded validated discovery records; do not imply playback eligibility."""
        ...
