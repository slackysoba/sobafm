# Jamendo metadata and admission boundary

The offline boundary in `jamendo_catalog.py` and `jamendo_admission.py` implements
the first slice of [ADR 0013](decisions/0013-jamendo-discovery-streaming.md).
It makes no network request, reads no configuration or credentials, persists
nothing and opens no audio. Runtime playback still uses the 167 admitted local
FMA recordings. The saved-source importer and FMA manual reviews are unchanged.

## Interfaces and ownership

| Interface | Owner and contract |
| --- | --- |
| `Policy(enabled=False)` | Trusted operator configuration only. Neither model output nor provider JSON is a configuration source. The fixed local version is `jamendo-stream-1`. |
| `Query(search, artist_name, name, tags)` | The following search/ranking slice constructs it **after semantic musical-intent validation**. This boundary validates field sizes, nonempty query and exclusion of URLs/markup; it does not decide whether text is musical. |
| `SearchWindow(query, policy, offset=0)` | One discovery attempt. `request()` returns a credential-free `Request` or typed `Refusal`. `receive(exact_request, response_bytes_or_refusal, observed_at)` consumes that request once. Only one may be outstanding; at most two pages of 100 are issued. |
| `SearchResult` | Immutable candidates, coverage, refusals, issued-page/received-byte counts and next offset. Candidates have a `Recording`, a ranking-only `Decision` and a material-content digest, never a stream address. |
| `parse_listing(row, expected_id=None)` | Complete untrusted row to an immutable, transient `Listing`, or a safe `AdmissionInputError.reason`. This is not the older importer, which drops unknown fields and therefore cannot provide this policy's complete evidence. |
| `evaluate(listing, policy, observed_at, purpose)` | A new immutable `Decision` binding key, canonical metadata digest, all normalized policy-evidence digest, version, reason, UTC refresh time and ranking/refresh purpose. `UNKNOWN` recording kind remains unknown: admission is explicitly provisional. |
| `refresh_request(key, policy)` / `refresh(request, bytes_or_refusal, ranked, policy, observed_at)` | Construct a by-ID request, then evaluate its new response. Result is `Refreshed` or `Refusal`; failed refresh never falls back to search metadata. This must run immediately before **every** audio open, including cache hits. |
| `Refreshed` | A new candidate/credit snapshot, an evidence-comparison flag and a transient stream URL excluded from its representation. It is not audio-integrity/readiness approval and is not a reusable playback permit. Only its candidate is suitable for a future bounded provenance journal; never serialize the wrapper or `Listing`. |

The future transport must use the fixed HTTPS tracks endpoint and encode the
request's parameters, inject the client ID separately, enforce the byte cap while
receiving, and return only a complete response or a safe failure. An issued request
is not evidence that a network call happened. Actual calls, bytes, quota and retained
operation ownership remain the transport's responsibility. Cancellation must keep
its owner until settlement; this pure boundary starts no task or cleanup clock.
Do not construct a fresh window to evade the attempt's two-page limit.

The search/ranking owner must wait until the window settles before ranking its
candidates: a conflicting duplicate on page two revokes the page-one candidate.
Repeated identical IDs are deduplicated; inconsistent and malformed duplicates
are quarantined throughout the window. Coverage is always partial, empty-window,
unavailable or disabled, never whole-catalog exhaustion. Windows contain at most
200 returned rows, inside the 256-record/source envelope. Offset is page-aligned
and below 153,600 (768 cycles times 200 positions); program cycle/history limits,
the three-attempt backoff and 512,000-character pooled matching ceiling belong to
the following discovery slice. No retry occurs here.

## Validation and evidence

Requests explicitly include singles and album tracks and ask for `mp32`, music
information and license flags. The [Jamendo tracks reference](https://developer.jamendo.com/v3.0/tracks)
documents these fields; they are not full-recording or complete-credit guarantees.
Each response is at most 1 MiB of UTF-8 JSON, with duplicate keys, nonfinite literal
numbers, excessive nesting and inconsistent result counts rejected. API errors and
nonempty warnings fail the page without copying their potentially sensitive text.

Identity, primary creator, title, source backlink, positive whole-second duration
(within the existing 86,400-second catalog bound), exact license and ordinary
stream route are required. Optional album fields may be absent or empty for singles.
There is no 1–15-minute filter. Missing/unknown credit structures and unsupported
fields fail closed; provider `approved`, policy or review fields cannot grant
admission. Known previews, excerpts, snippets, stems, loops and sample packs reject
in title, type, descriptions, details, tags and notices. Explicit warning lists,
rights-conflict wording and conflicting source/license links reject. Text-only
license claims need reconciliation to an exact URI rather than guessed equivalence.
Notice URL validation and conflict screening share token extraction. Scheme/host
case and an explicit HTTPS port do not bypass screening or change a matching
notice's meaning; license paths and recording IDs remain exact, and credits retain
the original supplied spelling.
This conservative text screening can refuse legitimate titles; it does not prove
the absence of undisclosed conflicts or recognize every possible natural-language
rights statement.

The finite license set follows the [Creative Commons license list](https://creativecommons.org/licenses/):
BY, BY-SA, BY-ND, BY-NC, BY-NC-SA and BY-NC-ND at generic 2.0/2.5,
unported 3.0 and international 4.0; generic 1.0 uses BY-ND-NC rather than
BY-NC-ND. Supported ports are US 3.0 and France 2.0. The exact supplied version and
jurisdiction remain in the URL and label. Other ports, versions, public-domain
instruments and unknown families are refused, not silently converted. The known
public HTTP Jamendo/CC backlinks are upgraded to HTTPS without changing their
path. No declaration here changes the accepted permission or certifies rights.

Every supplied creator and notice remains intact. Descriptions and explicit detail
text also enter credits so a credit-bearing sentence cannot be dropped as a hint.
Credits use the existing immutable `CreditSnapshot` and full safe renderer. Bounds
are 16 creators of 200 characters and 16 notices of 1,024 characters including
provider/composition notices; reject rather than truncate. Matching details keep
the existing 24 by 160-character limit. Unsupported markup, controls and links
with credentials, queries or fragments are refused in retained text.

The evidence digest covers every normalized identity, credit, descriptive and
license-flag input, and the validated ordinary-route category. Changes to tags or
other `Recording.details` invalidate it even when the older `metadata_digest`
stays the same. Known transport-only media/image links, waveform/statistics,
position and the download flag do not authorize admission and are not retained
or hashed. The ordinary route is currently the documented HTTPS
`prod-N.storage.jamendo.com/` track-ID query form with `format=mp32`; unfamiliar
routes fail closed. Credentials/ephemeral query values and the URL itself never
enter a candidate, decision, digest or error. This is request-shape validation,
not DNS, redirect, access or actual-stream qualification.

Refresh re-evaluates policy and both bindings. Changed valid notices or licenses
produce new credits and a new decision, with `previous_decision_matches=False`.
Identity or descriptive/content changes refuse the old ranked choice for bounded
continuation; the earlier immutable snapshot is never rewritten. Policy-version
changes similarly cannot inherit approval. `decision_matches` only compares
evidence; it cannot establish freshness or authorize cached audio. Leading and
later embedded notices are the stream producer's responsibility, including
discarding buffered audio on late conflict and preserving heard credits/debt.

## Following implementation requirements

Before production network transport becomes Ready, its issue must fix request
timeouts, credential injection/redaction, redirect and host validation, actual
call/byte/quota accounting, cancellation/close ownership and finite failure handling.
No client, SDK retry behavior or traffic allowance has been selected here.
Before a provenance journal is added, its issue must specify its required fields,
entry/byte retention limits, expiry and removal behavior. This slice adds no raw
response archive or catalog database. These follow-on requirements are recorded
on [#183](https://github.com/slackysoba/sobafm/issues/183), for coordinator ownership.

Search/model work and streaming work can independently consume these interfaces
after this slice is reviewed and merged. They must precede station integration.
No live Jamendo/Gemini/Discord request, audio access, build, native qualification,
listening result or release-readiness claim follows from the offline tests.
