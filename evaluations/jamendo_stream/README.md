# One bounded Jamendo stream access check

Prepared for [#184](https://github.com/slackysoba/sobafm/issues/184), following
[#178](https://github.com/slackysoba/sobafm/issues/178). **No live
permission is granted by this code or walkthrough.** Default invocation reads no
Client ID, starts no child and makes no request. The coordinator owns review,
merge, the separate go-ahead and any eventual execution. The successful earlier
metadata check and all five inconclusive stream-check invocations consumed their
permissions; none grants another request or retry. This offline diagnostic change for
[#201](https://github.com/slackysoba/sobafm/issues/201) needs independent review
and merge before the coordinator scopes one invocation under the
[standing necessary-test authorization](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6798078261).
The author must not run the live CLI or test unreviewed changes against Jamendo.

This is a transient access/framing experiment using **one fresh listing**, not
catalog admission, complete-recording validation, decoding or playback. Current
requirements permit Jamendo transient streaming and record direct source
permission. [ADR 0013](../../docs/decisions/0013-jamendo-discovery-streaming.md)
separately accepts provisional production admission with its own checks. This
experiment implements no production admission or playback. No
additional product/source-policy acceptance is assumed to authorize the narrowly
described experiment; the coordinator must record the concrete invocation scope
and its applicable maintainer authorization before execution.

## Request and source boundaries

Official documentation rechecked on 2026-11-11, with documentation reads only:

- [Tracks](https://developer.jamendo.com/v3.0/tracks) documents listing, lookup by ID,
  singles/album tracks, the distinct `audio` stream field and `audiodownload`
  download field, and `mp32` VBR MP3. First use one HTTPS GET on
  `api.jamendo.com:443`, `/v3.0/tracks/`, with `limit=10`, `order=id_desc`,
  `format=json`, `type=single albumtrack`, `audioformat=mp32`, `ccnc=true`,
  `durationbetween=1_480` and the operator's
  runtime-only ID. No fixed track ID, featured filter, musical query, pagination
  or full-catalog count. Select the first row in returned order passing every
  existing metadata/stream-route gate below. Refresh that one ID with one GET to
  the same endpoint and common fields, `id=<selected ID>`, `limit=1`, no `order`,
  `ccnc` or `durationbetween`.
- The [artists/tracks example](https://developer.jamendo.com/v3.0/artists/tracks)
  publishes the stream URL shape on `prod-1.storage.jamendo.com`, distinct from
  its download path. Accept only the refreshed `audio` URL with HTTPS, this exact
  host, no explicit port/userinfo/fragment, path `/`, and exactly three unique
  query fields: `trackid=<selected ID>`, `format=mp32`, `from=app-<operator ID>`. Reconstruct
  that checked query; never follow arbitrary source URLs or probe alternatives.
- [Tracks/file](https://developer.jamendo.com/v3.0/tracks/file) is a redirecting
  file route. It is not used. No download path, player page, alternate CDN,
  browser impersonation, proxy, Range, resume, redirect or fallback candidate.
- [Response codes](https://developer.jamendo.com/v3.0/response-codes) include
  application failures inside HTTP 200. Require code zero, status `success` or
  `succeed`, empty error/warning fields and an integer declared count matching
  the actual list length: 0–10 for listing, exactly one matching ID for refresh.
- [API terms](https://devportal.jamendo.com/api_terms_of_use) distinguish permitted
  non-commercial use, creator/provider credit and necessary transient caching
  from an offline library. No audio or raw metadata artifact is retained here.
  API access, a CC URL and the download flag do not establish playback rights.

The documented non-commercial license and duration filters narrow only this
probe's listing to a source-claimed subset of its already recognized policy.
They cannot establish eligibility or replace any local gate, exact refresh
binding or selected-track stream check. No `include=licenses` fix is assumed.
The experiment asks whether that subset supplies a candidate passing all
unchanged gates, or classifies the remaining safe refusal more precisely. Only
a passing candidate proceeds to bounded refresh/audio. Another all-refused
listing ends the experiment for evidence assessment, never an automatic retry.
The 1–480-second screen is evaluator sizing, not a production duration policy.

Metadata must contain a nonempty bounded title and artist name, numeric artist
identity, a consistent album/single shape, a 1–480-second integer duration, a
recognized CC license URL and the exact candidate's Jamendo backlink. If `type`
is returned, it must agree with that shape. The API need not return a type field;
album shape is only a consistency check, not proof of a whole recording. For a
single, either or both optional album fields may be absent or empty strings.
Null/non-string fields, a partially populated album, invalid album identity,
or a conflicting explicit type still refuse the row. An album track requires
both a positive numeric album ID and a nonempty bounded album name. The
duration ceiling is a conservative sizing screen (480 seconds at 320 kbit/s
is 19.2 MB); it is not a bitrate, size or completeness guarantee. The actual
byte/deadline limits always win. The download flag must be Boolean, but either
value is accepted because this is streaming, not a listener download feature.

The track ID must be a positive decimal string of at most 20 digits. Invalid
individual listing rows are skipped locally, never fetched. Empty listings or
no acceptable rows stop without a refresh. Once selected, no other candidate
can be tried, including after a refresh refusal, changed evidence or failed
stream. An oversized/malformed listing or application warning fails the entire
attempt; it is not silently truncated or treated as an empty catalog.

Both listing and refresh apply the same gates. The refreshed row must match
the selected ID and a transient SHA-256 of **all returned row fields**, including
optional notices/details and unknown fields. Only integer/string duration and
the already validated stream query ordering are normalized; object key order
does not matter. Even an incidental changed field stops conservatively. This
small access probe does not reconcile changes or certify notice completeness.
The evidence digest and raw rows/URLs are neither reported nor persisted.

The finite CC formats match the existing [production normalization](../../docs/jamendo-catalog.md#validation-and-evidence):
BY, BY-SA, BY-ND, BY-NC, BY-NC-SA and BY-NC-ND at 2.0/2.5/3.0/4.0;
1.0 uses historical BY-ND-NC instead of BY-NC-ND. The only supported ports
are US 3.0 and France 2.0, for the same six families at those versions.
The exact lowercase family/version/jurisdiction path must follow `/licenses/`,
with zero or one trailing slash. No percent-decoding, case folding of paths,
unknown combinations or public-domain instruments are accepted.

As in production, the authority must be exactly `creativecommons.org`, without
credentials, an explicit port (even `:443`/`:80`), a trailing dot or host-case
variation. The known lowercase `http://creativecommons.org/` prefix is accepted;
HTTPS scheme case follows the standard parser. Uppercase HTTP does not match
that existing production upgrade. Query/fragment delimiters are refused even
when empty, preserving the probe's stricter prior boundary. Whitespace, controls
and backslashes refuse before parsing. These are format checks only; no CC URL
is fetched, and the raw license field remains in the conservative whole-row
refresh binding. Even an accepted spelling change between listing and refresh
stops before streaming.

Licenses are recognized only as metadata evidence for this non-public access check. Missing/unrecognized
rights or credit fields stop before audio. This does not certify complete
notices, ownership, provenance, integrity, licensing or admission. No raw fields
are printed or published. Unexpected host/query/license shapes are inconclusive;
review them separately without trying another route.

## Exact bounds and evidence meaning

| Boundary | Behavior |
| --- | --- |
| Requests | Sequential: one listing GET, at most one by-ID refresh GET, then at most one stream GET; at most three requests/connections total. First DNS address only, one TCP/TLS attempt each. No HTTP/authentication retry, address fallback or redirect. No cookies, referrer, proxy or SDK retries. Honest `sobafm-jamendo-stream/1` User-Agent. |
| Protocol | HTTP/1.0, Host header, `Accept-Encoding: identity`, `Connection: close`, verified TLS/SNI, no ALPN advertisement or key logging. TLS without clean EOF fails closed, even if a length was delivered. |
| Response bytes | At most 256,000 **per metadata response** and 20,000,000 stream **decrypted HTTP bytes**, including status/headers/body: at most 20,512,000 total. No read crosses the remaining cap, no sentinel byte. Exact-cap responses are inconclusive. These are stricter than body-only budgets, not wire-traffic/kernel/TLS-buffer limits. |
| Memory | At most 16,384 header bytes and 8,192 bytes per read. Metadata alone is retained in a capped byte buffer for JSON parsing; audio blocks feed SHA-256 and are discarded. No 20 MB audio buffer, file, decoder or player. This evaluator does not claim production capacity qualification. |
| Framing | Require HTTP 200 and JSON or `audio/mpeg` as appropriate. Reject invalid/duplicate lengths, malformed/folded headers, partial Content-Range, encoded content and every Transfer-Encoding. Accept exact Content-Length plus EOF, or connection EOF with no length. Oversize declarations stop immediately; truncated/excess bodies fail. No custom chunk parser. |
| Time | One supervisor deadline covers startup, ID-file read, DNS, TLS, all three requests and parsing. Work stops by 117 seconds, leaving three seconds within the proposed 120-second total for cleanup. A child alarm independently covers its work, never reset between requests. On timeout/interrupt the parent kills its one child. |
| Cleanup debt | Every reap wait is bounded. The original three-second cleanup cutoff is never reset, including repeated interruption. If the OS delays reaping beyond it, the supervisor retains ownership until actual settlement and reports failed cleanup; elapsed time can then exceed 120 seconds. This is failed cleanup, never permission to keep requesting, retry or abandon the child. No claim guarantees kernel release. |
| Credential | Only the opted-in child opens a dedicated regular file: no symlink/FIFO, at most 160 bytes, ASCII ID of 6–128 allowed characters. Suspended shared test ID rejected. No environment-file loading, client secret/OAuth or ID value in arguments. Minimal child environment; stderr discarded and core-file limit zero. |
| Report | One sanitized ASCII JSON report, at most 100 KB. Fixed local reasons, at most ten candidate first-refusal entries (ordinal, fixed gate/condition and optional fixed license component/text detail), safe selected numeric ID/count diagnostics, Boolean refresh-validation flag, numeric request/status/byte facts, framing/EOF and stream-body SHA-256 only. No raw metadata/audio, headers, URL, title, artist, license text, key or exception message. An accidental key substring suppresses the entire child report. Keep all retained evidence for the invocation within 100 KB. |

The HTTP/1.0 choice follows the earlier [#175 review](https://github.com/slackysoba/sobafm/pull/175):
[RFC 9112 §6.1](https://www.rfc-editor.org/rfc/rfc9112.html#section-6.1) prohibits
transfer encoding unless the request indicates HTTP/1.1 or later. Closing a
connection alone does not prohibit chunks. Unsupported framing is an evaluator
limitation, not an account/source refusal. Python 3.14's maintained standard
socket, TLS, HTTP-header, JSON, hashing and subprocess libraries suffice; no new
dependency or production transport framework is added.

`stream_received` / exit 0 means only that the gated stream response ended with
accepted framing inside the bounds and the child settled. It does **not** mean
valid MP3, a whole recording, source correspondence, good music, production
memory/cleanup, Discord compatibility or release readiness. The hash identifies
the received body, not a trusted expected recording. On a failed stream it may
identify a received prefix; consult byte count, EOF and outcome together. A
supervisor-killed child cannot deliver its progress report, so request/byte
counts then remain unknown within the fixed upper bounds. Do not invent zeros.

Any failure is `inconclusive` / exit 1, with the original fixed reason preserved.
Keep the first report and exit status; never replace it with a rerun. HTTP/API
refusals, quota warnings, missing candidates and unexpected metadata all stop.
No outcome permits another request, wider URL allowlist, client disguise or
admission change. Invalid CLI arguments exit 2 without echoing them.

### Safe result interfaces and prior evidence

`search`, `metadata` (the refresh) and `stream` each report their own transfer
facts. `search_results` and `refresh_results` report the application code,
`declared_count_type`, `declared_count_value`, `container_type` and `list_count`.
Types are fixed JSON labels (`missing`, `null`, `boolean`, `integer`, `number`,
`string`, `list`, `object`); an unexamined response stays null. Declared values
are only integers within that response's 0–10 or 0–1 limit, or fixed labels
`not_integer`, `negative`, `above_limit`. Raw strings/floats/oversized integers
are never echoed. The actual list count is bounded by the metadata byte cap.
Separate reasons distinguish count type/value, non-list results, list overflow
and count/list disagreement. `selected_track_id` is null until selection;
`rejected_candidates` counts only rows examined and rejected before selection.
`metadata_validated` becomes true only after the refresh and consistency check.

`candidate_refusals` retains only the first failed gate for each examined and
refused listing row, in order, at most ten entries. `ordinal` is the one-based
list position, never a source ID. `gate` is one of `row`, `identity`, `title`,
`artist`, `album`, `duration`, `license`, `backlink`, `download_flag`,
`stream_route`. `condition` is `missing` (absent required field), `type` (wrong
JSON type), `shape` (empty/malformed/conflicting evidence), or `bound` (outside
an existing length/range limit). A non-object row is `row/type`; an unclassified
local candidate refusal is `row/shape`, without parsing its exception text.
The shared field gates use fixed `candidate_<gate>_<condition>` reasons during
refresh and do not add listing entries. Existing refresh identity/evidence
and response-envelope reasons remain separate. These
diagnostics contain no source values, IDs, URLs, value hashes, exceptions or
secrets. They describe a local check, not a provider's reason or policy verdict.

For `license/shape`, `license_component` identifies the first failing check in
this order: `text_shape`, `scheme`, `authority`, `query_or_fragment`, `path_shape`,
`family`, `version`, `jurisdiction`, `combination`. It is null for missing/type/bound
license failures, other gates and unclassified failures. The final combination
check rejects known parts used together in an unsupported family/version/port.
Listing refusals carry the component in their ordinal entry; a terminal refresh
license refusal carries it at report level, with the existing
`candidate_license_shape` reason and no new listing entry. The report-level
field is otherwise null. Only enum constants are emitted, never component
contents, source IDs, value hashes or parser exception text.

Only for `license/shape/text_shape`, `license_text_detail` gives the first
failure in this deterministic order:

1. `blank`: empty or entirely whitespace (`not value.strip()`). This check
   classifies only; it never trims or changes the value used for validation.
2. `whitespace`: any whitespace in a nonblank string, including whitespace
   controls such as tab/newline. This wins over other controls or backslashes.
3. `control`: any Unicode category C character, including ASCII controls/DEL,
   format characters, surrogates, private-use and unassigned characters. This
   wins over backslash, regardless of their positions in the string.
4. `backslash`: a backslash with none of the earlier failures.

The detail accompanies the component in listing entries or at report level for
a terminal refresh refusal; otherwise it is null. Missing/type/bound checks
still precede text shape. No string length, codepoint, source value, value hash,
URL or arbitrary error is retained. The rejection set and subsequent component
precedence are unchanged; there is no trimming, double-unescaping, invented
license or expanded rights recognition.

For `stream_route/shape` only, `route_diagnostic` holds a fixed record for the
already bounded string (at most 1,024 characters). It is null for missing/type/bound
failures, earlier gates, accepted routes and unclassified failures. Listing
refusals carry it in their ordinal entry; a terminal refresh refusal carries it
at report level with the unchanged `candidate_stream_route_shape` reason.

| Field | Fixed classifications |
| --- | --- |
| `non_ascii`, `whitespace`, `control`, `backslash` | `present` / `absent`; whitespace uses Unicode whitespace, controls are ASCII 0–31 and DEL, non-ASCII is separate |
| `url_parser` | `success` / `failure` |
| `scheme` | `https` / `http` / `empty` / `other` |
| `authority` | `exact` / `userinfo` / `port` / `userinfo_and_port` / `other`; only `exact` means the entire authority matched |
| `path` | `root` / `empty` / `other` |
| `fragment` | `present` / `absent`, including an empty fragment delimiter |
| `query_parser` | `success` / `field_limit` / `syntax`; the existing three-field limit precedes syntax |
| `query_keys` | `exact` / `mismatch` |
| `missing_keys`, `extra_keys`, `duplicate_keys` | Independent `present` / `absent` facts, so simultaneous key-set failures remain visible |
| `track_id`, `format`, `application_binding` | `match` / `mismatch` against the selected ID, mp32 and expected application binding |

Dependent fields are explicitly `not_evaluated` after parser failure; value
comparisons are also `not_evaluated` for missing or duplicate corresponding keys.
A failed query parse still permits scheme/authority/path/fragment diagnostics.
Text and raw fragment classes remain available after URL parser failure.
Bounded text failures may still be parsed for diagnostics: these fields describe
the standard parser's interpretation, including its own character stripping,
without changing or repairing the input used by the acceptance gate.

`first_component` preserves deterministic original-check precedence:
`text_shape`, `url_parser`, `query_parser`, `scheme`, `authority`, `path`,
`fragment`, `query_keys`, `track_id`, `format`, `application_binding`.
Text shape still rejects non-ASCII, ASCII characters 0–32 and DEL; backslash is
an independent diagnostic fact, not a new acceptance predicate. Multiple later
mismatches remain visible. No raw values, query names, credentials, IDs, lengths,
codepoints, value hashes or exception messages enter this record. The ten-entry
and 100,000-byte report caps and whole-report credential-substring suppression
remain. No trimming, extra unescaping, alternative URL or request is introduced.

The [first stream-check assessment](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6794455477)
preserves an **inconclusive** single-candidate count gate, zero audio exchange
and safe cleanup. Its report did not distinguish count type/value from container
shape/list count. Candidate 168's absence and the precise cause are unknown;
free streaming was not proven. Keep that attempt and its consumed permission.
The [second stream-check assessment](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6795405761)
preserves an **inconclusive** ten-row listing, ten local refusals, no selection,
zero refresh/audio requests and safe cleanup. No raw rows or per-row refusal
reasons survived. Existing saved reports cannot recover the historical refusal
causes. The optional-album omission gap was demonstrated synthetically; it is
**not the established cause** of those ten refusals. Keep both attempts unchanged,
with their consumed permissions. This revision improves only a future separately
authorized attempt after independent review and merge; it neither reinterprets
those results nor authorizes a retry.

The [third stream-check assessment and independent compatibility review](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6796833709)
preserves an **inconclusive** ten-row listing with ten `license/shape` refusals,
no selection, zero refresh/audio requests and safe cleanup. The actual license
values were not retained; the exact mismatch remains unknown. HTTP and HTTPS
already passed the old regex. Offline comparisons established missing US 3.0,
France 2.0, historical BY-ND-NC 1.0 and no-trailing-slash recognition, plus
incorrect acceptance of BY-NC-ND 1.0. None is an established a3 cause. This
revision corrects only those finite formats and safe diagnostics; a1/a2/a3 stay
unchanged, unsuccessful and permission-consumed. It establishes neither stream
access nor production admission, media validity or release readiness.

The [fourth stream-check assessment](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6798145415)
preserves an **inconclusive** ten-row listing: nine first refusals at
`license/shape/text_shape` and one at `duration/bound`, with no selection,
refresh or audio request. Its text diagnostic cannot distinguish blank,
whitespace, control/category-C or backslash, and no raw value survived. No
parser defect or historical cause is established. The new text detail and
listing subset apply only to a future reviewed invocation; a1/a2/a3/a4 and
their consumed permissions remain intact.

The [independent fifth-attempt review](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6798969703)
found no evidenced request/parser defect. The result remains inconclusive;
actual source strings and exact simultaneous mismatches are unknown. This
diagnostic-only change preserves a1–a5 and their consumed permissions. It makes
a future separately scoped experiment informative without expanding any route,
rights, credential, identity, refresh or request predicate.

## Operator walkthrough after review and merge

1. The coordinator identifies the reviewed merged source commit and this exact
   scope. No new account, paid plan or provider approval is part of preparation.
2. Reuse the operator's **existing dedicated Jamendo Client ID file**, including
   the one used for the earlier metadata/stream check, if it meets the bounded
   regular-file requirements above. No recreation is necessary. If none exists,
   the operator may create one in Notepad outside the repository containing only
   their existing ID, UTF-8 without BOM. Agents may inspect file metadata only;
   only the opted-in runtime reads contents. Never use a Client Secret, OAuth
   token, environment file, Discord or Gemini credential file. Do not paste the
   ID into chat, GitHub or a command. Give only its private Linux-visible path.
3. The coordinator records one concrete invocation as described below, tied to
   the reviewed merged commit and the standing necessary-test authorization.
   Preparation and earlier consumed permissions are not substitutes. The
   operator uses an existing locked Linux environment; no live invocation
   installs packages or changes the host.
4. Default demonstration, requiring no ID and no network:

   ```sh
   .venv/bin/python -I -B evaluations/jamendo_stream/probe.py
   ```

   Only after the coordinator's recorded scope, the command shape is:

   ```sh
   .venv/bin/python -I -B evaluations/jamendo_stream/probe.py \
     --send-to-jamendo /absolute/private/jamendo-stream-id.txt
   ```

   Replace only the private path. Do not run under shell/Python tracing or a
   debugger that captures locals. Do not call internal functions directly;
   the CLI supervisor owns the full deadline and child reaping.
5. Keep the sanitized report, exit status and actual source commit within the
   100 KB evidence cap. Stop after that one invocation. If cleanup is delayed,
   keep the supervisor alive until it observes settlement; a missing report or
   a timeout is not proof of release. The coordinator reports the exact result
   and preserves any failed verdict. No audio is retained to clean up.

### Next invocation scope — coordinator-owned after review and merge

Record the exact reviewed merged #201 source commit and one invocation using
the existing dedicated operator-owned Client ID file, without exposing its
contents. Bind the standing authorization to the listing's `ccnc=true` and
`durationbetween=1_480` subset, at most ten singles/album tracks ordered by
descending ID, selection of the first locally acceptable row, one refresh of
only that candidate, and one ordinary mp32 stream only after every gate and
whole-row consistency check passes. The hosts, routes, three-request ceiling,
byte/time/cleanup/report limits and all prohibitions above remain exact. The
purpose is access/refusal evidence only, not admission or release qualification.
Record the result and stop; no result authorizes a second invocation.

## Offline checks

Tests in this directory are explicitly invoked, outside ordinary CI collection:

```sh
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
uv run pyright evaluations/jamendo_stream
uv run pytest -q evaluations/jamendo_stream
```

Synthetic bytes/files, fake deadlines/children and loopback peers test request
order, fixed destinations, no retries/redirects/address fallback, actual header
parsing and prior framing failures, byte limits, optional single album fields,
contradictory album evidence, the supported/unsupported license matrix through
listing and refresh compared with production semantics, spelling/authority
boundaries, fixed bounded first-refusal component diagnostics, invalid
metadata, exact encoded subset parameters, ignored-filter local refusals,
text-detail precedence, every route diagnostic dimension and mixed failures,
parser-dependent not-evaluated states, unchanged route acceptance/reconstruction,
listing/refresh propagation, worst-case report size and credential-like marker
suppression, redaction, timeouts and retained cleanup debt. Loopback transport uses
a TLS stand-in;
real TLS-context defaults are checked separately. Nothing here establishes
actual provider/account/TLS/media behavior. No live request has been made by
this preparation.
