# Catalog relevance probe

The standalone [probe](../scripts/catalog_probe.py) prepares the research in
[#116](https://github.com/slackysoba/sobafm/issues/116), under
[M5](https://github.com/slackysoba/sobafm/issues/114). It changes no bot behavior and
makes no service, decoder or license-policy decision. Preparation is authorized;
the two-day measurement starts only once M4 is ready to ship.

## Prepare and approve the measurement

Run from the repository with the locked development environment. Use `python -m`
to reuse the repository's vetted Google Gen AI SDK, Pydantic and aiohttp dependencies.
There is no separate dependency installation and the probe does not read `.env`.

```sh
uv sync --locked
mkdir -p .local/catalog-probe
uv run python -m scripts.catalog_probe prepare --output .local/catalog-probe/manifest.json --pass-numerator 2 --pass-denominator 3
```

`prepare` is offline. It copies the 38 requests and current plans from the interpreter
evaluation set, with stable case ids. It never executes that evaluation's model calls
or property checks. Add about ten maintainer-selected requests to the manifest's
`cases` array, using unique ids, request text up to 200 characters, `expected`
(`new`, `refine`, or `not_music`), and `current` (a validated MusicPlan, required for
`refine`). Keep real requests free of personal data. Keep local inputs and reports
in the ignored `.local/` directory; never commit credentials or private requests.
The output file must be new; commands refuse to overwrite existing evidence.

Before running, the maintainer must approve on #116:

- M4 readiness, the model, the final manifest, and when the two-day timebox starts.
- The pass fraction, proposed as 2/3, with at least three fitting tracks in the
  top five for each passing music case.
- The denominator: the draft counts all 32 music cases plus added music requests.
  The six non-music/injection cases are separate refusal controls. All classification
  controls must pass for a go recommendation; failed music cases stay in the denominator.
- Attribute-filter retention through relaxation, and whether to collect stream HEAD
  observations. This is a draft measurement policy, not a runtime design decision.

Publish the final manifest SHA-256 (of `Manifest.model_dump_json()`), model and settings
with that approval. `prepare` prints the initial digest; after edits recompute it with:

```sh
uv run python -c "from pathlib import Path; import hashlib; from scripts.catalog_probe import Manifest; m=Manifest.model_validate_json(Path('.local/catalog-probe/manifest.json').read_text(encoding='utf-8')); print(hashlib.sha256(m.model_dump_json().encode()).hexdigest())"
```

The original checks measure Lyria controls that catalog search cannot guarantee:
exact BPM, musical key, density, brightness, drum muting and wordless vocals.
Keep their requests in the experiment and rate audible fit; do not reuse their
numerical assertions. `speed` is coarse, and catalog `vocal` can include lyrics.
Prior plans supply musical context for relative requests; they are not catalog plans.
This probe measures relevance, not the future catalog refinement implementation.

## Run after the gates are satisfied

The operator supplies `GEMINI_API_KEY` and `JAMENDO_CLIENT_ID` in the process environment.
The maintainer registers their own Jamendo application; the probe never registers
accounts. Google may review free-tier prompts, as described in the README.
No paid/live calls are part of the preparatory work.

```sh
uv run python -m scripts.catalog_probe run --manifest .local/catalog-probe/manifest.json --output .local/catalog-probe/report.json --m4-ready --approval-reference https://github.com/slackysoba/sobafm/issues/116#issuecomment-APPROVED_COMMENT_ID --inspect-streams
```

Replace the comment placeholder with the actual numeric approval comment id. These
flags record operator attestations; the script does not verify GitHub approval or M4.
`--model` overrides `SOBAFM_GEMINI_MODEL`, otherwise the repository's current default
`gemini-3.5-flash-lite` is used. Approve any model change before measurement.

One structured Gemini call interprets each request. Pydantic validates the raw JSON,
and invalid replies never fall back to a raw-text catalog search. Refusals and safety
blocks are recorded. A known non-music control never reaches Jamendo, even if Gemini
misclassifies it. Calls are sequential, with six seconds between cases, no automatic
retries, and 30-second request timeouts. A new writable report is reserved before the first call and updated after each
completed case. An error stops the run, retaining a partial report, which cannot
produce a go recommendation. Reruns need a new output file and
must be disclosed on the issue, rather than replacing earlier evidence.

For each music query, search uses `tags` (all), then `fuzzytags` (any), then descriptive
`search` text, advancing only for a successful empty response. Speed, vocal/instrumental
and acoustic/electric filters stay in all three stages. Artist and work names become
English descriptions (AI-5). Search includes singles and album tracks (`type=all`),
requests music metadata, keeps Jamendo's default relevance/popularity behavior, and
returns at most five tracks. It applies no NC/ND exclusion pending #117's decision.
A nonempty result, even one with fewer than three tracks, is rated as returned.

The report includes validated metadata, license labels, interpretation time, each
search stage's latency, HTTP status, and Jamendo `headers.status`/`code`.
Successful empty stages remain in the report if a later stage fails. The failed
attempt includes its latency and a safe error description; unavailable HTTP status,
Jamendo status/code and result count are `null`, not inferred as success or zero.
Successful attempts retain their original serialized form and report/rating binding.
Search relaxation stops at that failure. The report omits raw
error messages/warnings, download URLs, stream URLs, and unrelated response fields.
`view` derives each listening link from the numeric track id and credits the artist
and Jamendo. Unknown licenses remain `unknown`, never inferred as permissive.
Library logging and warnings are suppressed during CLI execution because they may
quote response text or credentials; failures use fixed descriptions or safe error
codes. Free-text URLs are omitted regardless of HTTP(S) or `www` casing. Export
refuses an unexpected echo of either supplied credential, including percent-encoded
values with mixed hex casing or nested encoding.

`--inspect-streams` additionally looks up `mp32` for each result and sends HEAD requests
for both `mp31` and `mp32`. It records status chains, whether a URL contains `client_id`
or its supplied value, and Content-Length when available. It follows at most five
redirects, only between HTTPS `storage.jamendo.com` hosts on port 443, with no userinfo;
untrusted redirects are recorded and never followed. It does not GET, decode, play
or save audio. HEAD may be unsupported or omit Content-Length: those observations
remain unavailable, not zero. HEAD behavior is not proof of GET behavior. Without
this flag the report has no file-size or redirect evidence. The extra format lookups
and HEAD calls count against the operator's service limits; no automatic alternate
catalog comparison is included.

## Listen, rate and report

```sh
uv run python -m scripts.catalog_probe view --report .local/catalog-probe/report.json
uv run python -m scripts.catalog_probe ratings --report .local/catalog-probe/report.json --output .local/catalog-probe/ratings.json
uv run python -m scripts.catalog_probe summarize --report .local/catalog-probe/report.json --ratings .local/catalog-probe/ratings.json
```

The maintainer listens using each Jamendo track page and sets every `fits` value in
the ratings file to `true` or `false`. `null` means unrated. Ratings refer to case and
track ids and are bound to the complete report's SHA-256; duplicate, missing, foreign
rows or a changed report are rejected. Preserve the original report, especially its
manifest and pass mark. Incomplete runs and unrated tracks yield `pending`.

The summary lists each case's classification and fitting-track count, the music-case
denominator, the threshold recommendation, and the license mix and NC/ND shares over
track appearances (a repeated track counts once per case). Unknown licenses remain in
the denominator and are reported separately. Inspect the case reports for search
latency and stream observations. The summary is an arithmetic recommendation based
on the maintainer's ratings, not an independent listening judgment or approval of M5.

Post on #116 the approved manifest/model/pass mark, full results table, listening
ratings, license and stream observations, incomplete/error attempts, elapsed timebox
and the maintainer's go/no-go. Leave #116 open until that live evidence exists.
Independent review of the script and passing offline checks are preparatory evidence.

## Primary sources verified for preparation

Checked on 2026-10-06:

- [Jamendo tracks API](https://developer.jamendo.com/v3.0/tracks): tags/fuzzytags,
  relevance behavior, filters, `type=all`, metadata and `mp31`/`mp32` formats.
- [Jamendo response codes](https://developer.jamendo.com/v3.0/response-codes): status,
  code and error envelope. The overview calls success `succeed`, while the tracks
  example uses `success`; validation accepts both with code 0, and `failed` with nonzero code.
- [Jamendo document format](https://developer.jamendo.com/v3.0/docs): every reply has
  `headers` and `results`; HTTPS is recommended.
- [Jamendo API terms](https://devportal.jamendo.com/api_terms_of_use): non-commercial
  use, creator/provider attribution and backlinks, and no offline audio product.
- [Google structured output guide](https://ai.google.dev/gemini-api/docs/structured-output)
  and [official Python SDK](https://github.com/googleapis/python-genai#json-response-schema):
  schema-constrained JSON and local validation. The SDK documents `generate_content`
  with `response_json_schema`, which the existing locked interpreter uses.

These sources justify probe calls only. Service adoption, licenses/crossfades and
runtime decoding remain with #117/#118; accepted decisions and requirements are unchanged.
