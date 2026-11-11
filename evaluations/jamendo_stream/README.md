# One bounded Jamendo stream access check

Prepared for [#184](https://github.com/slackysoba/sobafm/issues/184), following
[#178](https://github.com/slackysoba/sobafm/issues/178). **No live
permission is granted by this code or walkthrough.** Default invocation reads no
Client ID, starts no child and makes no request. The coordinator owns review,
merge, the separate go-ahead and any eventual execution. The successful earlier
metadata check and first stream-check invocation consumed their permissions;
neither grants another request or retry.

This is a transient access/framing experiment using **one fresh listing**, not
catalog admission, complete-recording validation, decoding or playback. Current
requirements permit Jamendo transient streaming and record direct source
permission. [ADR 0013](../../docs/decisions/0013-jamendo-discovery-streaming.md)
separately accepts provisional production admission with its own checks. This
experiment implements no production admission or playback. No
additional product/source-policy acceptance is assumed to authorize the narrowly
described experiment; the fresh explicit live go-ahead remains necessary.

## Request and source boundaries

Official documentation rechecked on 2026-11-11, with documentation reads only:

- [Tracks](https://developer.jamendo.com/v3.0/tracks) documents listing, lookup by ID,
  singles/album tracks, the distinct `audio` stream field and `audiodownload`
  download field, and `mp32` VBR MP3. First use one HTTPS GET on
  `api.jamendo.com:443`, `/v3.0/tracks/`, with `limit=10`, `order=id_desc`,
  `format=json`, `type=single albumtrack`, `audioformat=mp32` and the operator's
  runtime-only ID. No fixed track ID, featured filter, musical query, pagination
  or full-catalog count. Select the first row in returned order passing every
  existing metadata/stream-route gate below. Refresh that one ID with one GET to
  the same endpoint and common fields, `id=<selected ID>`, `limit=1`, no `order`.
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

Metadata must contain a nonempty bounded title and artist name, numeric artist
identity, a consistent album/single shape, a 1–480-second integer duration, a
recognized CC license URL and the exact candidate's Jamendo backlink. If `type`
is returned, it must agree with that shape. The API need not return a type field;
album shape is only a consistency check, not proof of a whole recording. The
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

The six standard CC families and versions 1.0/2.0/2.5/3.0/4.0 are recognized
only as metadata evidence for this non-public access check. Missing/unrecognized
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
| Report | One sanitized ASCII JSON report, at most 100 KB. Fixed local reasons, safe selected numeric ID/count diagnostics, Boolean refresh-validation flag, numeric request/status/byte facts, framing/EOF and stream-body SHA-256 only. No raw metadata/audio, headers, URL, title, artist, license text, key or exception message. An accidental key substring suppresses the entire child report. Keep all retained evidence for the invocation within 100 KB. |

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

The [first stream-check assessment](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6794455477)
preserves an **inconclusive** single-candidate count gate, zero audio exchange
and safe cleanup. Its report did not distinguish count type/value from container
shape/list count. Candidate 168's absence and the precise cause are unknown;
free streaming was not proven. Keep that attempt and its consumed permission.
This revision improves the next separately approved attempt; it does not
reinterpret the old result or authorize a retry.

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
3. The coordinator obtains and records the fresh go-ahead below, tied to the
   reviewed commit. Preparation and the previous metadata permission are not
   substitutes. The operator uses an existing locked Linux environment; no live
   invocation installs packages or changes the host.
4. Default demonstration, requiring no ID and no network:

   ```sh
   .venv/bin/python -I -B evaluations/jamendo_stream/probe.py
   ```

   Only after the fresh go-ahead, the command shape is:

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

### Proposed next go-ahead — not permission already received

> I authorize one invocation of the reviewed and merged #184 Jamendo stream
> evaluator at source commit `<exact reviewed commit>`, using my existing
> dedicated operator-owned Client ID file without showing the ID to the agent.
> Allow one HTTPS current listing of at most ten singles/album tracks ordered
> by descending ID, deterministic selection of its first acceptable row, one
> by-ID refresh of only that candidate, and only if all gates and evidence
> consistency pass, one ordinary mp32 stream GET. Use only api.jamendo.com and
> the exact prod-1.storage.jamendo.com route described here, at most three
> requests/connections. Limit each metadata response to 256,000 decrypted HTTP
> bytes and the stream to 20,000,000 bytes, with 117 seconds of total work and three
> seconds for cleanup within 120 seconds. If cleanup misses its deadline,
> preserve the failure and retain ownership until the child is reaped; make no
> further request. No retries, redirects, range/resume, alternate candidate,
> proxy, disguised client, decoding, playback, retained audio, model or Discord
> calls. Keep only the sanitized report and invocation evidence, at most 100 KB.
> Stop on refusal, warning, authentication/rate-limit error, unexpected URL,
> invalid metadata/framing or any bound. This authorizes no spending, admission,
> source-policy change, production integration or second invocation.

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
parsing and prior framing failures, byte limits, invalid metadata, redaction,
timeouts and retained cleanup debt. Loopback transport uses a TLS stand-in;
real TLS-context defaults are checked separately. Nothing here establishes
actual provider/account/TLS/media behavior. No live request has been made by
this preparation.
