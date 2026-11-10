"""Offline regressions for safe credit rendering, pagination and retained views."""

import json
from dataclasses import replace

import pytest

from sobafm.catalog import (
    Availability,
    CreditSnapshot,
    Recording,
    RecordingKey,
    RecordingKind,
    Source,
    parse_catalog,
)
from sobafm.credits import (
    MAX_PAGES,
    MESSAGE_LIMIT,
    PAGE_BODY_LIMIT,
    CreditRejected,
    CreditViews,
    PageRefusal,
    RenderedCredits,
    render_credits,
)

LINK = "https://example.invalid/synthetic/track"


def recording(
    title: str = "Synthetic Song",
    notices: tuple[str, ...] = ("Synthetic test; no audio exists.",),
    **changes: object,
) -> Recording:
    result = Recording(
        key=RecordingKey(Source.CCMIXTER, "one"),
        title=title,
        primary_artist="Synthetic Artist",
        creators=("Synthetic Artist", "Synthetic Contributor"),
        provenance=LINK,
        kind=RecordingKind.COMPLETE,
        kind_evidence=LINK,
        duration_seconds=240,
        availability=Availability.AVAILABLE,
        composition=None,
        composer=None,
        facts=(),
        credits=CreditSnapshot(
            "synthetic-credit-v1",
            notices,
            "CC BY 4.0",
            "https://creativecommons.org/licenses/by/4.0/",
        ),
    )
    return replace(result, **changes)


def text(credits: RenderedCredits) -> str:
    return "\n".join(credits.pages)


def width(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_renders_complete_credits_with_links_without_previews() -> None:
    credits = render_credits(recording())
    assert credits.key == RecordingKey(Source.CCMIXTER, "one")
    assert credits.snapshot_id == "synthetic-credit-v1"
    assert credits.pages == (
        "Title: Synthetic Song\n"
        "Creators: Synthetic Artist, Synthetic Contributor\n"
        f"Source: ccMixter <{LINK}>\n"
        "License: CC BY 4.0 <https://creativecommons.org/licenses/by/4.0/>\n"
        "Notices:\n"
        "• Synthetic test; no audio exists.\n"
        "Credit snapshot: synthetic\\-credit\\-v1",
    )
    page = credits.page(1)
    assert page.text.endswith("\nPage 1/1")
    assert not page.allow_mentions
    assert page.suppress_previews
    for number in (0, 2):
        with pytest.raises(ValueError):
            credits.page(number)


def test_renders_parsed_catalog_snapshot_at_maximum_supported_size() -> None:
    record: dict[str, object] = {
        "recording_id": "max",
        "title": "t" * 200,
        "primary_artist": "c00" + "a" * 197,
        "creators": [f"c{n:02}" + "a" * 197 for n in range(16)],
        "provenance": LINK,
        "kind": "complete",
        "kind_evidence": LINK,
        "duration_seconds": 240,
        "availability": "available",
        "composition": None,
        "composer": None,
        "facts": [],
        "credits": {
            "snapshot_id": "s" * 128,
            "notices": [f"{n:02} " + "n" * 1021 for n in range(16)],
            "license_label": "l" * 200,
            "license_url": LINK,
        },
    }
    payload = {
        "schema": "sobafm-catalog-1",
        "synthetic": True,
        "partitions": [
            {"source": "fma", "state": "complete", "note": "Synthetic.", "recordings": [record]}
        ],
    }
    parsed = parse_catalog(json.dumps(payload)).partitions[0].recordings[0]
    credits = render_credits(parsed)
    joined = "".join(credits.pages)
    assert len(credits.pages) <= MAX_PAGES
    assert "Source: Free Music Archive <" + LINK + ">" in joined
    for notice in parsed.credits.notices if parsed.credits else ():
        assert notice in joined


@pytest.mark.parametrize(
    ("hostile", "rendered"),
    [
        ("@everyone", "\\@everyone"),
        ("<@123456789>", "\\<\\@123456789\\>"),
        ("**bold** __u__ ~~s~~ `c` ||spoiler||", None),
        ("[click](https://evil.example/x)", "\\[click\\]\\(https\\://evil.example/x\\)"),
        ("https://evil.example/token?key=secret", "https\\://evil.example/token?key=secret"),
        ("# heading > quote - list", "\\# heading \\> quote \\- list"),
        ("back\\slash", "back\\\\slash"),
        ("a‮evil⁦b‏c؜d", "aevilbcd"),
        ("line\nbreak\ttab\x00nul\x85", "line break tab nul "),
        ("a\u2028b\u2029c", "a b c"),
    ],
)
def test_escapes_hostile_provider_text_and_strips_bidi(hostile: str, rendered: str | None) -> None:
    for item in (
        recording(title=hostile),
        recording(notices=(hostile,)),
        recording(creators=(hostile,)),
    ):
        output = text(render_credits(item))
        if rendered is not None:
            assert rendered in output
        assert not any(c in output for c in "‪‫‬‭‮⁦⁧⁨")
        assert "⁩" not in output and "‏" not in output and "؜" not in output
        # Every markdown-significant character from provider text is escaped.
        for character in "*_~`|":
            assert character not in output.replace("\\" + character, "")
        assert "](" not in output


@pytest.mark.parametrize(
    "link",
    [
        "http://example.invalid/track",
        "https://placeholder:placeholder@example.invalid/track",  # pragma: allowlist secret
        "https://example.invalid/track?token=secret",
        "https://example.invalid/track#frag",
        "https://example.invalid:8443/track",
        "https://localhost/track",
        "https://example.invalid/tr>ack",
        "https://example.invalid/träck",
        "javascript:alert(1)",
    ],
)
def test_rejects_unvalidated_links(link: str) -> None:
    with pytest.raises(CreditRejected):
        render_credits(recording(provenance=link))
    base = recording()
    assert base.credits is not None
    with pytest.raises(CreditRejected):
        render_credits(recording(credits=replace(base.credits, license_url=link)))


def test_rejects_missing_or_unpaired_license_and_missing_snapshot() -> None:
    base = recording()
    assert base.credits is not None
    with pytest.raises(CreditRejected):
        render_credits(recording(credits=None))
    with pytest.raises(CreditRejected):
        render_credits(recording(credits=replace(base.credits, license_url=None)))
    with pytest.raises(CreditRejected):
        render_credits(recording(credits=replace(base.credits, license_label=None)))
    unlicensed = replace(base.credits, license_label=None, license_url=None, notices=())
    assert "License" not in text(render_credits(recording(credits=unlicensed)))


def test_pagination_keeps_every_notice_complete_and_bounded() -> None:
    notices = tuple(f"Notice {n}: " + "x" * 1000 for n in range(16))
    credits = render_credits(recording(notices=notices))
    assert 1 < len(credits.pages) <= MAX_PAGES
    for number in range(1, len(credits.pages) + 1):
        page = credits.page(number)
        assert width(page.text) <= MESSAGE_LIMIT
        assert width(credits.pages[number - 1]) <= PAGE_BODY_LIMIT
        assert page.text.endswith(f"Page {number}/{len(credits.pages)}")
    joined = "".join(credits.pages)
    for notice in notices:
        assert notice.replace(":", "\\:") in joined


def test_lines_split_without_breaking_escapes_or_surrogates() -> None:
    notice = ("\U0001f3b5*" * 1000)[:1024]
    credits = render_credits(recording(notices=(notice,)))
    assert len(credits.pages) > 1
    for body in credits.pages:
        assert not body.endswith("\\")
        assert width(body) <= PAGE_BODY_LIMIT
    assert "".join(credits.pages).count("\U0001f3b5") == notice.count("\U0001f3b5")
    assert "".join(credits.pages).count("\\*") == notice.count("*")


def test_page_boundary_exactly_fills_body_limit() -> None:
    header = render_credits(recording(notices=())).pages[0].split("\n")[:-1] + ["Notices:"]
    used = sum(width(line) + 1 for line in header)
    notice = "y" * (PAGE_BODY_LIMIT - used - width("\u2022 ") - 1)
    snapshot = "Credit snapshot: synthetic\\-credit\\-v1"

    exact = render_credits(recording(notices=(notice,))).pages
    assert exact == ("\n".join([*header, "\u2022 " + notice]), snapshot)
    assert width(exact[0]) == PAGE_BODY_LIMIT - 1

    over = render_credits(recording(notices=(notice + "y",))).pages
    assert over == ("\n".join([*header, "\u2022 " + notice]), "y\n" + snapshot)


def test_oversized_credits_reject_instead_of_truncating() -> None:
    notices = tuple("\\" * 1024 for _ in range(16))
    with pytest.raises(CreditRejected, match="exceed"):
        render_credits(recording(notices=notices))


def test_views_are_caller_bound_bounded_and_expiring() -> None:
    notices = tuple(f"Notice {n} " + "z" * 1000 for n in range(4))
    credits = render_credits(recording(notices=notices))
    total = len(credits.pages)
    views = CreditViews(max_views=2, lifetime_seconds=60)
    view = 10
    first = views.open(view, credits, caller_id=111, now=10.0)
    assert first == credits.page(1)
    assert views.page(view, 222, 1, now=11.0) is PageRefusal.WRONG_CALLER
    assert views.page(view, 111, 0, now=11.0) is PageRefusal.OUT_OF_RANGE
    assert views.page(view, 111, total + 1, now=11.0) is PageRefusal.OUT_OF_RANGE
    assert views.page(view, 111, total, now=69.999) == credits.page(total)
    assert views.page(view, 111, 1, now=70.0) is PageRefusal.UNAVAILABLE
    assert len(views) == 0

    first_view, second_view, third_view = 11, 12, 13
    views.open(first_view, credits, 1, now=80.0)
    views.open(second_view, credits, 2, now=81.0)
    views.open(third_view, credits, 3, now=82.0)
    assert len(views) == 2
    assert views.page(first_view, 1, 1, now=83.0) is PageRefusal.UNAVAILABLE
    assert views.page(second_view, 2, 1, now=83.0) == credits.page(1)
    assert views.page(third_view, 3, 1, now=83.0) == credits.page(1)
    assert views.page(999, 3, 1, now=83.0) is PageRefusal.UNAVAILABLE


def test_views_validate_injected_time_and_callers() -> None:
    credits = render_credits(recording())
    views = CreditViews()
    views.open(1, credits, 1, now=5.0)
    with pytest.raises(ValueError):
        views.open(2, credits, 1, now=4.0)
    with pytest.raises(ValueError):
        views.page(1, 1, 1, now=float("nan"))
    with pytest.raises(ValueError):
        views.open(2, credits, 0, now=6.0)
    with pytest.raises(ValueError):
        CreditViews(max_views=0)
    with pytest.raises(ValueError):
        CreditViews(lifetime_seconds=0)
