"""Opt-in: send real requests to Gemini over a catalog and print what it ranks.

Nothing is sent without `--send-to-gemini`. With `--dry-run` it reports the prompt size
and makes no call. It plays no audio, joins no Discord channel and changes no state.
See this folder's README for cost and data notes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sobafm.catalog import (
    Availability,
    CatalogInputError,
    CreditSnapshot,
    Recording,
    RecordingKey,
    RecordingKind,
    Source,
    eligible,
    parse_catalog,
    parse_reviews,
)
from sobafm.discord_adapter import GEMINI_KEY_VARIABLE
from sobafm.gemini_match import (
    MODEL,
    GeminiMatcher,
    Matched,
    MatchFailed,
    checked_request,
    render_songs,
)

MAX_REQUESTS = 20
SHOWN = 5
LINK = "https://example.invalid/synthetic/gemini-matching"

# Invented songs covering a wide range, so matching can be judged before a real catalog exists.
SYNTHETIC: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    (
        "Calliope at Low Tide",
        "The Rust Carnival",
        212,
        ("genre: dark cabaret", "instrument: accordion", "tag: eerie", "tag: circus"),
    ),
    (
        "Sea of Tranquility Waltz",
        "Apollo Drift",
        305,
        ("genre: space ambient", "instrument: synth pad", "tag: weightless", "speed: low"),
    ),
    (
        "Rain on the 9:15",
        "Platform Nine",
        188,
        ("genre: lofi hip hop", "tag: rainy", "tag: melancholy", "bpm: 72"),
    ),
    ("Neon Overdrive", "Grid Runner", 241, ("genre: synthwave", "tag: driving", "bpm: 118")),
    (
        "Kitchen Table Blues",
        "Mae Holloway Trio",
        198,
        ("genre: acoustic blues", "instrument: slide guitar", "vocal gender: female"),
    ),
    (
        "Fourth Street Swing",
        "The Hep Cats",
        176,
        ("genre: swing jazz", "instrument: clarinet", "tag: cheerful", "speed: high"),
    ),
    (
        "Iron Cathedral",
        "Molten Choir",
        332,
        ("genre: doom metal", "instrument: distorted guitar", "tag: heavy"),
    ),
    (
        "Paper Lanterns",
        "Yuki Aoyama",
        260,
        ("genre: solo piano", "tag: gentle", "tag: nostalgic", "vocal or instrumental: instrumental"),
    ),
    (
        "Sunday Pancakes",
        "Little Fern",
        154,
        ("genre: indie folk", "instrument: ukulele", "tag: cheerful", "tag: morning"),
    ),
    ("Block Party 1987", "MC Static", 203, ("genre: old school hip hop", "tag: rap", "bpm: 98")),
    (
        "Cuivre et Poussière",
        "Les Frères Morel",
        222,
        ("genre: French chanson", "language: fr", "instrument: brass"),
    ),
    (
        "The Last Lighthouse",
        "Northern Orchestra of Nowhere",
        411,
        ("genre: orchestral", "tag: dramatic", "tag: cinematic"),
    ),
    ("Bass Cadet", "Funk Academy", 233, ("genre: funk", "instrument: slap bass", "tag: groove")),
    (
        "Glass Forest",
        "Aura Lund",
        380,
        ("genre: ambient", "instrument: bells", "tag: dreamy", "tag: spacious"),
    ),
    (
        "Tractor Beam Lullaby",
        "Cow Abduction Society",
        190,
        ("genre: novelty", "instrument: theremin", "tag: absurd", "tag: alien"),
    ),
    (
        "Monsoon Market",
        "Tabla Collective",
        274,
        ("genre: world fusion", "instrument: tabla", "instrument: sitar", "tag: rain"),
    ),
    ("Deadline", "Caffeine Protocol", 167, ("genre: drum and bass", "tag: energetic", "bpm: 174")),
    (
        "Grandfather's Porch",
        "Old Pine Ramblers",
        205,
        ("genre: bluegrass", "instrument: banjo", "tag: warm"),
    ),
    (
        "Velvet Elevator",
        "The Lobby Boys",
        216,
        ("genre: lounge", "instrument: vibraphone", "tag: laid back"),
    ),
    (
        "Thunder in Her Teeth",
        "Riot Verse",
        181,
        ("genre: punk rock", "vocal gender: female", "tag: angry", "speed: high"),
    ),
)


def synthetic_recordings() -> tuple[Recording, ...]:
    return tuple(
        Recording(
            key=RecordingKey(Source.JAMENDO, f"synthetic-{number}"),
            title=title,
            primary_artist=artist,
            creators=(artist,),
            provenance=LINK,
            kind=RecordingKind.COMPLETE,
            kind_evidence=LINK,
            duration_seconds=seconds,
            availability=Availability.AVAILABLE,
            composition=None,
            composer=None,
            facts=(),
            credits=CreditSnapshot(f"synthetic-{number}", ("Synthetic; no audio exists.",), None, None),
            details=details,
        )
        for number, (title, artist, seconds, details) in enumerate(SYNTHETIC)
    )


def load_recordings(catalog: Path | None, reviews: Path | None) -> tuple[Recording, ...]:
    if catalog is None and reviews is None:
        return synthetic_recordings()
    if catalog is None or reviews is None:
        raise CatalogInputError("--catalog and --reviews must be given together")
    return eligible(
        parse_catalog(catalog.read_text(encoding="utf-8")),
        parse_reviews(reviews.read_text(encoding="utf-8")),
    )


async def run(recordings: tuple[Recording, ...], requests: list[str], key: str) -> int:
    matcher = GeminiMatcher(recordings, key)
    failures = 0
    try:
        for request in requests:
            print(f"\nREQUEST: {request}")
            try:
                result = await matcher.match(request)
            except MatchFailed as exc:
                failures += 1
                print(f"  failed: {exc.kind}")
                continue
            if not isinstance(result, Matched):
                print("  refused: not a music request")
                continue
            for place, choice in enumerate(result.ranked[:SHOWN], start=1):
                song = choice.recording
                print(f"  {place}. {choice.fit.value:<5} {song.title} by {song.primary_artist}")
    finally:
        await matcher.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", nargs="+", help="request text, as a listener would type it")
    parser.add_argument("--catalog", type=Path, help="prepared snapshot; default is synthetic")
    parser.add_argument("--reviews", type=Path, help="trusted admission reviews for --catalog")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--send-to-gemini", action="store_true", help="make real, billable calls")
    mode.add_argument("--dry-run", action="store_true", help="report the prompt size; no calls")
    args = parser.parse_args()
    if len(args.requests) > MAX_REQUESTS:
        parser.error(f"at most {MAX_REQUESTS} requests per run")
    try:
        requests = [checked_request(text) for text in args.requests]
        recordings = load_recordings(args.catalog, args.reviews)
        songs = render_songs(recordings)
    except (CatalogInputError, OSError, UnicodeError) as exc:
        parser.error(str(exc) if isinstance(exc, CatalogInputError) else "input file unavailable")
    print(f"model={MODEL} songs={len(recordings)} song_characters={len(songs)}")
    if args.dry_run:
        return 0
    key = os.environ.pop(GEMINI_KEY_VARIABLE, "").strip()
    if not key:
        parser.error(f"{GEMINI_KEY_VARIABLE} must be set in this shell's environment")
    return asyncio.run(run(recordings, requests, key))


if __name__ == "__main__":
    sys.exit(main())
