from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from sobafm.catalog import CatalogQuery, CatalogTrack, CreditRecord, License, Notice, safe_link


def credit(**changes: Any) -> CreditRecord:
    return CreditRecord.model_validate(
        {
            "track_id": "42",
            "title": "Song",
            "artist": "Artist",
            "license": License.BY,
            "attribution_parties": ("Artist", "Designated creator"),
            "notices": (
                Notice(kind="copyright", text="Copyright 2026 Artist"),
                Notice(
                    kind="disclaimer",
                    text="Supplied without warranty",
                    links=("https://example.org/notice",),
                ),
            ),
            "source_urls": ("https://www.jamendo.com/track/42",),
            "source_digest": "a" * 64,
        }
        | changes
    )


def discovered(**changes: Any) -> CatalogTrack:
    return CatalogTrack.model_validate(
        {
            "id": "42",
            "title": "Song",
            "artist": "Artist",
            "duration": 120,
            "license": License.BY,
            "audio": SecretStr("https://storage.jamendo.com/?trackid=42"),
        }
        | changes
    )


def test_discovery_is_not_a_complete_notice_record() -> None:
    assert not discovered().eligible
    complete = discovered().with_credits(credit())
    assert complete.eligible
    assert complete.credits is not None
    rendered = "\n".join(complete.credits.blocks())
    assert "Designated creator" in rendered
    assert "Copyright 2026 Artist" in rendered
    assert "Supplied without warranty" in rendered
    assert "https://example.org/notice" in rendered
    assert "CC BY 3.0 Unported" in rendered
    assert "https://www.jamendo.com/track/42" in rendered
    assert "Music provided by [Jamendo]" in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"track_id": "43"},
        {"title": "Other song"},
        {"artist": "Other artist"},
        {"license": License.BY_NC},
    ],
)
def test_credits_cannot_transfer_between_tracks_or_rights(changes: dict[str, Any]) -> None:
    if "track_id" in changes:
        changes = changes | {
            "source_urls": (f"https://www.jamendo.com/track/{changes['track_id']}",)
        }
    with pytest.raises(ValidationError, match="conflict"):
        discovered(credits=credit(**changes))


def test_complete_credits_are_immutable() -> None:
    record = credit()
    with pytest.raises(ValidationError):
        record.title = "Replacement"
    with pytest.raises(ValidationError):
        record.notices[0].text = "Discarded notice"


@pytest.mark.parametrize(
    "changes", [{"source_urls": ()}, {"source_digest": ""}, {"attribution_parties": ()}]
)
def test_credits_require_bound_sources_and_attribution(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        credit(**changes)


def test_raw_credit_record_bound_rejects_instead_of_truncating() -> None:
    with pytest.raises(ValidationError, match="raw bound"):
        credit(notices=(Notice(kind="license", text="界" * 11_000),))


def test_escaped_utf16_credit_bound_counts_markup_and_non_bmp_characters() -> None:
    with pytest.raises(ValidationError, match="rendering bound"):
        credit(notices=(Notice(kind="license", text="*" * 8100),))
    with pytest.raises(ValidationError, match="rendering bound"):
        credit(notices=(Notice(kind="license", text="😀" * 8100),))


def test_page_limit_preserves_whole_links_and_rejects_overflow() -> None:
    links = tuple(f"https://example.org/{index}/" + "x" * 900 for index in range(12))
    with pytest.raises(ValidationError, match="page bound"):
        credit(notices=(Notice(kind="license", text="Required links", links=links),))


def test_credit_sources_include_the_actual_track_and_revalidate_copied_evidence() -> None:
    with pytest.raises(ValidationError, match="exact track"):
        credit(source_urls=("https://www.jamendo.com/track/43",))
    copied = credit().model_copy(update={"source_digest": "unqualified"})
    with pytest.raises(ValidationError):
        discovered().with_credits(copied)


def test_direct_track_construction_cannot_bypass_safe_audio_validation() -> None:
    with pytest.raises(ValidationError, match="unsafe audio"):
        discovered(audio=SecretStr("https://127.0.0.1/private"))


def test_credit_text_is_escaped_and_bidi_controls_removed_without_losing_notices() -> None:
    record = credit(
        notices=(Notice(kind="attribution", text="@everyone [claim](https://bad.org)\u202e"),)
    )
    text = "\n".join(record.blocks())
    assert "\\[claim\\]" in text
    assert "https\\://bad\\.org" in text
    assert "\u202e" not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://creativecommons.org/licenses/by/4.0/",
        "https://creativecommons.org/licenses/by/3.0/us/",
        "https://creativecommons.org/licenses/by/3.0/deed.en",
        "https://creativecommons.org/licenses/by/3.0/?credential=secret",
        "https://creativecommons.org/licenses/by/3.0/#x",
        "https://creativecommons.org.evil.org/licenses/by/3.0/",
        "https://user@creativecommons.org/licenses/by/3.0/",
        "CC BY",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    ],
)
def test_rejects_versions_ports_generic_flags_and_lookalike_licenses(url: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        License.from_url(url)


@pytest.mark.parametrize("license_", list(License))
@pytest.mark.parametrize(
    ("scheme", "host", "suffix"),
    [
        ("https", "creativecommons.org", "/"),
        ("http", "creativecommons.org", "/"),
        ("https", "www.creativecommons.org", ""),
    ],
)
def test_only_reviewed_license_aliases_canonicalize(
    license_: License, scheme: str, host: str, suffix: str
) -> None:
    assert License.from_url(f"{scheme}://{host}/licenses/{license_.value}/3.0{suffix}") is license_


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "https://server.local/",
        "https://127.0.0.1/",
        "https://10.0.0.1/",
        "https://169.254.169.254/",
        "https://2130706433/",
        "http://127.1/",
        "https://user:secret@example.org/",
        "https://example.org/?client_id=secret",
        "https://example.org/#secret",
        "file:///notice",
        "javascript:alert(1)",
        "https://example.org/%0a",
        "https://example.org/\n",
        "https://example.org\\@127.0.0.1/",
        "https://example.org/)(mention",
        "https://[::1]/",
        "https://example.org:444/",
        "https://example.org/" + "x" * 1000,
        "https://localhost.localdomain/",
        "http://0x7f.1/",
        "http://0x7f.0x0.0x0.0x1/",
    ],
)
def test_rejects_unsafe_or_credential_bearing_credit_links(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        safe_link(url)


@pytest.mark.parametrize(
    "query",
    [
        {"tags": ()},
        {"tags": ("jazz", "jazz")},
        {"tags": ("@everyone",)},
        {"tags": ("https://example.org",)},
        {"tags": ("jazz",) * 7},
        {"tags": ("jazz",), "search": "https://example.org"},
        {"tags": ("jazz",), "extra": "value"},
    ],
)
def test_query_cannot_forward_unbounded_or_unsupported_input(query: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CatalogQuery(**query)
