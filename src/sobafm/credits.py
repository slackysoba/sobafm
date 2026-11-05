"""Bounded private rendering of source-bound catalog credits; no Discord or clock access.

Credits come only from a recording's immutable `CreditSnapshot` in `catalog.py`.
Provider text is escaped as literal Discord markdown after bidirectional controls
are stripped. Only credential-free HTTPS links are rendered, inside angle brackets
so they produce no preview. Complete credits are split into a bounded number of
pages; credits that need more pages are rejected rather than truncated.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from sobafm.catalog import (
    CatalogInputError,
    CreditSnapshot,
    Recording,
    RecordingKey,
    Source,
    credential_free_url,
)

# Discord rejects message content above 2,000 UTF-16 code units.
MESSAGE_LIMIT = 2_000
# Room reserved on every page for the "Page n/m" footer.
_FOOTER_RESERVE = 32
PAGE_BODY_LIMIT = MESSAGE_LIMIT - _FOOTER_RESERVE
MAX_PAGES = 16
MAX_VIEWS = 256
VIEW_LIFETIME_SECONDS = 600.0

_SOURCE_NAMES = {
    Source.JAMENDO: "Jamendo",
    Source.CCMIXTER: "ccMixter",
    Source.FMA: "Free Music Archive",
}

# Unicode bidirectional formatting characters (UAX #9 explicit and implicit marks).
_BIDI_CONTROLS = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
# Characters with Discord markdown, mention, timestamp, emoji or autolink meaning.
# Escaping ':' also prevents provider text such as "https://..." from becoming a link.
_MARKDOWN = frozenset("\\*_~`|<>#-[]():@!")
# RFC 3986 characters, excluding '<' and '>' which would end the no-preview wrapper.
_LINK_CHARACTERS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


class CreditRejected(ValueError):
    """Complete safe credits cannot be rendered; the candidate must not be admitted."""


@dataclass(frozen=True)
class CreditPage:
    """One private page. The Discord layer sends it ephemerally with these flags."""

    text: str
    number: int
    total: int
    allow_mentions: bool = field(default=False, init=False)
    suppress_previews: bool = field(default=True, init=False)


@dataclass(frozen=True)
class RenderedCredits:
    """Complete escaped credit pages for one exact credit snapshot."""

    key: RecordingKey
    snapshot_id: str
    pages: tuple[str, ...]

    def page(self, number: int) -> CreditPage:
        if type(number) is not int or not 1 <= number <= len(self.pages):
            raise ValueError("credit page does not exist")
        body = self.pages[number - 1]
        return CreditPage(f"{body}\nPage {number}/{len(self.pages)}", number, len(self.pages))


def _width(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _escaped(text: str) -> list[str]:
    """Return display units for provider text; each unit is kept whole across pages."""
    units: list[str] = []
    for character in text:
        if character in _BIDI_CONTROLS:
            continue
        if unicodedata.category(character) in {"Cc", "Zl", "Zp"}:
            units.append(" ")
        elif character in _MARKDOWN:
            units.append("\\" + character)
        else:
            units.append(character)
    return units


def escaped_text(text: str) -> str:
    """Provider text made safe to show in a Discord message."""
    return "".join(_escaped(text))


def _link(value: str) -> str:
    try:
        url = credential_free_url(value)
    except CatalogInputError as exc:
        raise CreditRejected("credit link is not a credential-free HTTPS URL") from exc
    if _LINK_CHARACTERS.fullmatch(url) is None:
        raise CreditRejected("credit link contains characters outside RFC 3986")
    return f"<{url}>"


def _lines(recording: Recording, credits: CreditSnapshot) -> list[list[str]]:
    lines: list[list[str]] = [
        ["Title: ", *_escaped(recording.title)],
        ["Creators: ", *_escaped(", ".join(recording.creators))],
        [f"Source: {_SOURCE_NAMES[recording.key.source]} ", _link(recording.provenance)],
    ]
    if credits.license_label is not None:
        if credits.license_url is None:
            raise CreditRejected("license label has no link")
        lines.append(
            ["License: ", *_escaped(credits.license_label), " ", _link(credits.license_url)]
        )
    elif credits.license_url is not None:
        raise CreditRejected("license link has no label")
    if credits.notices:
        lines.append(["Notices:"])
        lines.extend(["• ", *_escaped(notice)] for notice in credits.notices)
    lines.append(["Credit snapshot: ", *_escaped(credits.snapshot_id)])
    return lines


def _paginate(lines: list[list[str]]) -> tuple[str, ...]:
    pages: list[str] = []
    current: list[str] = []
    used = 0

    def flush() -> None:
        nonlocal current, used
        pages.append("".join(current).rstrip("\n"))
        if len(pages) > MAX_PAGES:
            raise CreditRejected(f"complete credits exceed {MAX_PAGES} pages")
        current, used = [], 0

    # Fill pages continuously so maximum catalog credits fit. A line may continue on
    # the next page; units are whole escapes or code points and never split.
    for units in lines:
        for unit in units:
            width = _width(unit)
            if used + width + 1 > PAGE_BODY_LIMIT:
                flush()
            current.append(unit)
            used += width
        current.append("\n")
        used += 1
    if current:
        flush()
    return tuple(pages)


def render_credits(recording: Recording) -> RenderedCredits:
    """Render all credits of an admitted recording, or reject it without truncation."""
    credits = recording.credits
    if credits is None:
        raise CreditRejected("credit snapshot missing")
    pages = _paginate(_lines(recording, credits))
    return RenderedCredits(recording.key, credits.snapshot_id, pages)


class PageRefusal(StrEnum):
    UNAVAILABLE = "credit view is unknown or expired"
    WRONG_CALLER = "credit view belongs to another member"
    OUT_OF_RANGE = "credit page does not exist"


@dataclass(frozen=True)
class _View:
    credits: RenderedCredits
    caller_id: int
    expires_at: float


class CreditViews:
    """Retained caller-bound credit views with a fixed count and creation-based expiry.

    Time is injected as monotonic seconds. Opening a view when full evicts expired
    views, then the oldest view; an evicted view is reported as unavailable.
    """

    def __init__(
        self, *, max_views: int = MAX_VIEWS, lifetime_seconds: float = VIEW_LIFETIME_SECONDS
    ) -> None:
        if type(max_views) is not int or not 1 <= max_views <= MAX_VIEWS:
            raise ValueError(f"max_views must be from 1 to {MAX_VIEWS}")
        if not math.isfinite(lifetime_seconds) or not 0 < lifetime_seconds <= 900:
            raise ValueError("lifetime_seconds must be positive and at most 900")
        self._max_views = max_views
        self._lifetime = lifetime_seconds
        self._views: dict[int, _View] = {}
        self._next_id = 1
        self._now = -math.inf

    def __len__(self) -> int:
        return len(self._views)

    def _advance(self, now: float) -> None:
        if not math.isfinite(now) or now < self._now:
            raise ValueError("injected monotonic time must be finite and nondecreasing")
        self._now = now
        for view_id in [i for i, v in self._views.items() if v.expires_at <= now]:
            del self._views[view_id]

    def open(self, credits: RenderedCredits, caller_id: int, now: float) -> tuple[int, CreditPage]:
        """Retain a view for one caller and return its identifier and first page."""
        if type(caller_id) is not int or caller_id <= 0:
            raise ValueError("caller_id must be a positive Discord identifier")
        self._advance(now)
        if len(self._views) >= self._max_views:
            del self._views[next(iter(self._views))]
        view_id = self._next_id
        self._next_id += 1
        self._views[view_id] = _View(credits, caller_id, now + self._lifetime)
        return view_id, credits.page(1)

    def page(
        self, view_id: int, caller_id: int, number: int, now: float
    ) -> CreditPage | PageRefusal:
        """Return a page only to the caller that opened the unexpired view."""
        self._advance(now)
        view = self._views.get(view_id)
        if view is None:
            return PageRefusal.UNAVAILABLE
        if caller_id != view.caller_id:
            return PageRefusal.WRONG_CALLER
        if type(number) is not int or not 1 <= number <= len(view.credits.pages):
            return PageRefusal.OUT_OF_RANGE
        return view.credits.page(number)
