"""Offline catalog probe tests: synthetic model replies and a loopback HTTP server only."""

import argparse
import json
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from google.genai import types
from pydantic import SecretStr, ValidationError

from scripts import catalog_probe as probe
from tests.eval.test_interpretation import CASES

KEY = SecretStr("local-placeholder-credential")
QUERY = probe.CatalogQuery(title="Soft jazz", tags=["jazz", "piano"], speed="low")


def track_data(id_: str = "123") -> dict[str, object]:
    return {
        "id": id_,
        "name": "Quiet piano",
        "artist_name": "Example artist",
        "duration": 120,
        "license_ccurl": "http://creativecommons.org/licenses/by-nc-nd/3.0/",
        "musicinfo": {"tags": {"genres": ["jazz"], "instruments": ["piano"], "vartags": ["calm"]}},
        "audio": "https://prod-1.storage.jamendo.com/?client_id=local-placeholder-credential",
        "audiodownload": "https://untrusted.example/credential-bearing-url",
        "shareurl": "https://untrusted.example/credential-bearing-url",
    }


def reply_data(tracks: list[dict[str, object]], *, code: int = 0) -> dict[str, object]:
    return {
        "headers": {
            "status": "failed" if code else "success",
            "code": code,
            "results_count": len(tracks),
            "error_message": KEY.get_secret_value(),
        },
        "results": tracks,
    }


@asynccontextmanager
async def server(handler: Callable[[web.Request], Awaitable[web.Response]]) -> AsyncGenerator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    await web.SockSite(runner, sock).start()
    try:
        yield url
    finally:
        await runner.cleanup()


class Gemini:
    def __init__(self, text: str) -> None:
        self.text = text
        self.contents = ""
        self.config: types.GenerateContentConfig | None = None

    @property
    def models(self) -> Gemini:
        return self

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        self.contents, self.config = contents, config
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(parts=[types.Part(text=self.text)]),
                    finish_reason=types.FinishReason.STOP,
                )
            ]
        )


def report() -> probe.Report:
    cases = [probe.Case(id=f"case-{i}", request="jazz", expected="new") for i in range(3)]
    cases.append(probe.Case(id="negative", request="hello", expected="not_music"))
    manifest = probe.Manifest(cases=cases, pass_numerator=2, pass_denominator=3)
    results = [
        probe.CaseResult(
            id=c.id,
            outcome="music",
            kind="new",
            query=QUERY,
            interpretation_s=0.1,
            tracks=[
                probe.ResultTrack(
                    id=str(j),
                    name="Piano",
                    artist="Example",
                    duration_s=120,
                    tags=["jazz"],
                    license="CC BY-NC-ND 3.0",
                )
                for j in range(1, 6)
            ],
        )
        for c in cases[:-1]
    ]
    results.append(
        probe.CaseResult(id="negative", outcome="not_music", kind="not_music", interpretation_s=0.1)
    )
    return probe.Report(
        manifest=manifest,
        model=probe.MODEL,
        sdk="offline",
        started_utc="2026-10-07T00:00:00Z",
        approval_reference="https://github.com/slackysoba/sobafm/issues/116#issuecomment-1",
        inspect_streams=False,
        results=results,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"tags": []},
        {"tags": ["jazz+junk"]},
        {"tags": ["jazz", "jazz"]},
        {"speed": "120"},
        {"vocalinstrumental": "wordless"},
        {"extra": "action"},
    ],
)
def test_query_rejects_unsupported_fields_and_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        probe.CatalogQuery.model_validate(QUERY.model_dump() | change)


def test_catalog_cases_retain_context_and_do_not_reuse_lyria_expectations() -> None:
    cases = [
        probe.Case(id=f"eval-{i:02}", request=c.request, expected=c.kind, current=c.current)
        for i, c in enumerate(CASES, 1)
    ]
    assert len(cases) == 38
    assert sum(c.expected == "not_music" for c in cases) == 6
    assert all(c.current is not None for c in cases if c.expected == "refine")
    assert "exact BPM" in probe.INSTRUCTION
    assert "checks" not in cases[0].model_dump()


def test_refinement_requires_context() -> None:
    with pytest.raises(ValidationError):
        probe.Case(id="x", request="faster", expected="refine")


def test_steps_do_not_mix_and_or_text_or_drop_explicit_attributes() -> None:
    steps = probe.search_steps(QUERY)
    assert steps == [
        ("all_tags", {"speed": "low", "tags": "jazz+piano"}),
        ("any_tag", {"speed": "low", "fuzzytags": "jazz+piano"}),
        ("text", {"speed": "low", "search": "jazz piano"}),
    ]


async def test_interpretation_validates_raw_json_and_sends_only_musical_context() -> None:
    gemini = Gemini(json.dumps({"kind": "new", "query": QUERY.model_dump()}))
    case = probe.Case(id="x", request="jazz", expected="new")
    answer = await probe.interpret(gemini, probe.MODEL, case)
    assert answer is not None
    assert answer.query == QUERY
    assert json.loads(gemini.contents) == {"request": "jazz", "current_plan": None}
    assert gemini.config is probe.CONFIG
    gemini.text = '{"kind":"new","query":{"title":"broken"}}'
    with pytest.raises(ValidationError):
        await probe.interpret(gemini, probe.MODEL, case)


def test_safety_blocks_and_inconsistent_refusals() -> None:
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(finish_reason=types.FinishReason.SAFETY)]
    )
    assert probe.read_interpretation(response) is None
    with pytest.raises(ValidationError):
        probe.Interpretation(kind="not_music", query=QUERY)


async def test_relaxes_only_empty_results_and_keeps_relevance_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(dict(request.query))
        return web.json_response(reply_data([track_data()] if "search" in request.query else []))

    async with server(handler) as url, aiohttp.ClientSession() as session:
        monkeypatch.setattr(probe, "TRACKS_URL", url)
        tracks, attempts = await probe.search(session, KEY, QUERY)
    assert len(tracks) == 1
    assert [a.stage for a in attempts] == ["all_tags", "any_tag", "text"]
    assert all(c["client_id"] == KEY.get_secret_value() for c in calls)
    assert all(
        c["include"] == "musicinfo" and c["type"] == "all" and "order" not in c for c in calls
    )
    assert "tags" not in calls[-1]
    assert "fuzzytags" not in calls[-1]


@pytest.mark.parametrize("code", [0, 5, 6])
async def test_nonempty_or_failed_search_never_relaxes(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response(reply_data([] if code else [track_data()], code=code))

    async with server(handler) as url, aiohttp.ClientSession() as session:
        monkeypatch.setattr(probe, "TRACKS_URL", url)
        _, attempts = await probe.search(session, KEY, QUERY)
    assert calls == 1
    assert attempts[0].code == code
    assert KEY.get_secret_value() not in attempts[0].model_dump_json()


async def test_http_redirect_and_oversized_body_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/large":
            return web.Response(body=b" " * (probe.MAX_BODY + 1))
        return web.Response(status=302, headers={"Location": "https://untrusted.example"})

    async with server(handler) as url, aiohttp.ClientSession() as session:
        monkeypatch.setattr(probe, "TRACKS_URL", url)
        with pytest.raises(probe.ProbeError, match="redirect refused"):
            await probe.fetch_tracks(session, KEY, {})
        monkeypatch.setattr(probe, "TRACKS_URL", url + "/large")
        with pytest.raises(probe.ProbeError, match="too large"):
            await probe.fetch_tracks(session, KEY, {})


@pytest.mark.parametrize(
    "bad",
    [
        {"duration": "120"},
        {"duration": -1},
        {"id": "../../"},
        {"artist_name": None},
    ],
)
def test_track_validation_is_strict(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        probe.Reply.model_validate(reply_data([track_data() | bad]))


async def test_reports_only_canonical_links_and_metadata() -> None:
    async with aiohttp.ClientSession() as session:
        result = await probe.report_track(
            session, KEY, probe.Track.model_validate(track_data()), False
        )
    assert result.link == "https://www.jamendo.com/track/123"
    assert result.license == "CC BY-NC-ND 3.0"
    assert result.tags == ["jazz", "piano", "calm"]
    assert "client_id" not in result.model_dump_json()
    assert "untrusted.example" not in result.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "http://prod-1.storage.jamendo.com/",
        "https://storage.jamendo.com.evil.example/",
        "https://127.0.0.1/",
        "https://user:pass@storage.jamendo.com/",
        "https://storage.jamendo.com:444/",
        "https://storage.jamendo.com/#fragment",
        "garbage",
    ],
)
def test_stream_allowlist_rejects_unsafe_urls(url: str) -> None:
    assert not probe.stream_url_allowed(url)


async def test_stream_redirects_head_only_and_blocks_untrusted_next_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        methods.append(request.method)
        if request.path == "/first":
            return web.Response(
                status=302, headers={"Location": "/second?client_id=local-placeholder-credential"}
            )
        if request.path == "/blocked":
            return web.Response(status=302, headers={"Location": "https://untrusted.example/"})
        return web.Response(headers={"Content-Length": "12345"})

    async with server(handler) as url, aiohttp.ClientSession() as session:

        def allowed(target: str) -> bool:
            return target.startswith(url + "/")

        monkeypatch.setattr(probe, "stream_url_allowed", allowed)
        observed = await probe.inspect_stream(session, url + "/first", KEY, "mp31")
        blocked = await probe.inspect_stream(session, url + "/blocked", KEY, "mp32")
    assert observed.bytes == 12345
    assert observed.carries_client_id
    assert observed.statuses == [302, 200]
    assert observed.outcome == "ok"
    assert blocked.outcome == "unsupported_url"
    assert blocked.statuses == [302]
    assert methods == ["HEAD", "HEAD", "HEAD"]
    assert KEY.get_secret_value() not in observed.model_dump_json()


async def test_negative_case_never_calls_jamendo_even_if_misclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "TRACKS_URL", "https://invalid.example/")
    async with aiohttp.ClientSession() as session:
        result = await probe.measure_case(
            Gemini(json.dumps({"kind": "new", "query": QUERY.model_dump()})),
            probe.MODEL,
            session,
            KEY,
            probe.Case(id="negative", request="hello", expected="not_music"),
            False,
        )
    assert result.outcome == "music"
    assert result.tracks == []
    assert result.attempts == []


async def test_run_is_gated_before_credentials_or_network() -> None:
    with pytest.raises(probe.ProbeError, match="M4 readiness"):
        await probe.run(argparse.Namespace(m4_ready=False, approval_reference=None))


def test_rating_threshold_uses_all_music_cases_and_failures_stay_in_denominator() -> None:
    result = report()
    ratings = probe.rating_template(result)
    assert probe.summarize(result, ratings)["recommendation"] == "pending"
    for rating in ratings.tracks:
        rating.fits = rating.case_id in ("case-0", "case-1") and int(rating.track_id) <= 3
    summary = probe.summarize(result, ratings)
    assert summary["recommendation"] == "go"
    assert summary["music_cases"] == 3
    assert summary["fitting_cases"] == 2
    assert summary["nc_share"] == 1
    assert summary["nd_share"] == 1
    ratings.tracks[0].fits = False
    assert probe.summarize(result, ratings)["recommendation"] == "no-go"


def test_partial_runs_cannot_pass_and_refusal_controls_can_fail_a_run() -> None:
    result = report()
    result.results = result.results[:1]
    ratings = probe.rating_template(result)
    for rating in ratings.tracks:
        rating.fits = True
    assert probe.summarize(result, ratings)["recommendation"] == "pending"
    result = report()
    result.results[-1] = probe.CaseResult(
        id="negative", outcome="music", kind="new", query=QUERY, interpretation_s=0
    )
    ratings = probe.rating_template(result)
    for rating in ratings.tracks:
        rating.fits = True
    assert probe.summarize(result, ratings)["recommendation"] == "no-go"


def test_ratings_reject_duplicates_missing_rows_and_altered_report() -> None:
    result = report()
    ratings = probe.rating_template(result)
    ratings.tracks.append(ratings.tracks[0])
    with pytest.raises(probe.ProbeError, match="duplicated"):
        probe.summarize(result, ratings)
    ratings = probe.rating_template(result)
    ratings.tracks.pop()
    with pytest.raises(probe.ProbeError, match="exactly"):
        probe.summarize(result, ratings)
    ratings = probe.rating_template(result)
    result.manifest.pass_numerator = 1
    with pytest.raises(probe.ProbeError, match="another report"):
        probe.summarize(result, ratings)


def test_exports_refuse_credential_echo_and_overwrite(tmp_path: Path) -> None:
    result = report()
    result.results[0].tracks[0].name = KEY.get_secret_value()
    path = tmp_path / "report.json"
    with pytest.raises(probe.ProbeError, match="credential echo"):
        probe.write_model(path, result, (KEY,))
    assert not path.exists()
    probe.write_model(path, probe.rating_template(report()))
    with pytest.raises(FileExistsError):
        probe.write_model(path, probe.rating_template(report()))


async def test_run_reserves_output_and_keeps_completed_cases_on_client_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import AbstractAsyncContextManager

    output = tmp_path / "report.json"
    manifest_path = tmp_path / "manifest.json"
    manifest = probe.Manifest(
        cases=[probe.Case(id="case", request="jazz", expected="new")],
        pass_numerator=2,
        pass_denominator=3,
    )
    probe.write_model(manifest_path, manifest)
    monkeypatch.setenv("GEMINI_API_KEY", KEY.get_secret_value())
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "placeholder-client-id")

    @asynccontextmanager
    async def aio() -> AsyncGenerator[Gemini]:
        yield Gemini("")
        raise RuntimeError("synthetic close failure")

    class Client:
        @property
        def aio(self) -> AbstractAsyncContextManager[Gemini]:
            return aio()

    def client(*, api_key: str, http_options: types.HttpOptions) -> Client:
        assert api_key == KEY.get_secret_value()
        assert http_options is probe.GEMINI_HTTP
        return Client()

    async def measure(
        gemini: probe.Gemini,
        model: str,
        session: aiohttp.ClientSession,
        client_id: SecretStr,
        case: probe.Case,
        inspect: bool,
    ) -> probe.CaseResult:
        assert output.exists()
        initial = probe.Report.model_validate_json(output.read_text(encoding="utf-8"))
        assert initial.results == []
        return probe.CaseResult(id=case.id, outcome="error", interpretation_s=0.1, error="fixed")

    monkeypatch.setattr(probe.genai, "Client", client)
    monkeypatch.setattr(probe, "measure_case", measure)
    args = argparse.Namespace(
        m4_ready=True,
        approval_reference="https://github.com/slackysoba/sobafm/issues/116#issuecomment-1",
        manifest=manifest_path,
        output=output,
        model=probe.MODEL,
        inspect_streams=False,
    )
    with pytest.raises(RuntimeError, match="close failure"):
        await probe.run(args)
    preserved = probe.Report.model_validate_json(output.read_text(encoding="utf-8"))
    assert len(preserved.results) == 1
    assert preserved.results[0].outcome == "error"
    assert KEY.get_secret_value() not in output.read_text(encoding="utf-8")
    assert probe.summarize(preserved, probe.rating_template(preserved))["recommendation"] == "no-go"
    with pytest.raises(probe.ProbeError, match="already exists"):
        await probe.run(args)
