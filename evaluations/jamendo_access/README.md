# One-request Jamendo metadata check

Prepared for [#173](https://github.com/slackysoba/sobafm/issues/173). **No live
request is authorized or recorded by this change.** This is an optional check of
the documented tracks API, not a source integration or a claim that every
Jamendo playback route requires registration. It does not run in ordinary CI,
and no production module imports it. Python 3.14 on Linux, standard library only;
no new dependency, image, audio file or application configuration.

## What this check answers

One operator-owned application ID either obtains a valid metadata response or
gets a bounded failure. The report contains only local status/reason names, HTTP
status, application code, counts, numeric recording IDs and field-presence flags.
Presence means that a key exists, even if empty or invalid; it does not validate
credits, licenses, audio URLs, completeness or eligibility.

Metadata success does **not** establish audio access, broad-catalog size or
coverage, permission, complete recordings, music quality, streaming reliability,
latency, capacity or deployment readiness. Nothing is admitted, persisted in the
catalog, played or sent to a model. The broader design and numeric proposals in
[#171](https://github.com/slackysoba/sobafm/issues/171) remain unaccepted.

## Existing evidence and route choice

- [#119's result](https://github.com/slackysoba/sobafm/issues/119#issuecomment-6655798005)
  was HTTP 200 with application code 11: the shared documentation test ID was
  suspended. It made one metadata request and zero stream requests. The
  [final disposition](https://github.com/slackysoba/sobafm/issues/119#issuecomment-6656744227)
  requires fresh authorization for future work. This runner rejects that shared
  ID locally, including at its callable network entry.
- The [earlier pilot](../catalog/three-source-pilot.md) retained two identities
  from the official tracks documentation, not a successful live lookup or
  stream. The retained older documentation-page reader used different bounds;
  the original #119 executable was not found in the available repository history
  or retained project evaluation directories. Its published result is preserved.
  The FMA fetcher handles archives/audio and permits resumes, so it is not reused.
- The maintainer reports having used Jamendo without an application process
  before. That working route has not been identified by this evaluation. A
  published direct stream URL, supported broad discovery, API registration,
  service approval and permitted automated playback are different facts.
  Documentation examples contain stream URLs, but neither the examples nor this
  metadata check prove how to discover and play current recordings without an ID.
  Do not fetch those URLs or reverse-engineer a player as part of this check.
- [Jamendo authentication](https://developer.jamendo.com/v3.0/authentication)
  says the documented API uses a client ID and newly created applications default
  to read-only. It describes approval for upgrading to read-and-write; that is
  different from creating a read-only application. This does not prove the
  operator's current account flow or that registration is the only viable route.
- Jamendo's [general terms](https://licensing.jamendo.com/en/legal/termsofuse)
  explicitly describe embedding a code/widget/player on other websites under
  the applicable license and terms. That is evidence of a distinct supported
  presentation route; the page does not establish a bot discovery/streaming
  interface or its credential requirements. The
  [radio-stream documentation](https://developer.jamendo.com/v3.0/radios/stream)
  separately warns that its stream link is broken. Neither route was requested.

Proceed with the walkthrough below **only if the maintainer chooses this API
check**. The coordinator owns the alternative-route investigation and any later
source/admission decision. This directory does not change source policy.

## Fixed request and limits

The destination is the HTTPS tracks method on `api.jamendo.com`, port 443. The
fixed GET uses `format=json`, `limit=10`, `type=single albumtrack` and
`include=licenses musicinfo`, plus the privately supplied `client_id`. No search
text, pagination, full-count query or audio URL is followed. The
[tracks reference](https://developer.jamendo.com/v3.0/tracks) documents these
parameters and separates the stream and download fields.

| Boundary | Behavior |
| --- | --- |
| Default | No child, credential read, DNS lookup or request; prints `disabled` and exits 0. Invalid arguments exit 2 without echoing them. |
| Connections/requests | First DNS address only, at most one TCP/TLS attempt and one GET. No address fallback, HTTP retry, redirect, proxy, cookie, OAuth or authentication retry. An honest fixed tool User-Agent; no browser impersonation or referrer. |
| Time | Supervisor waits at most 10 seconds for the isolated child, including its startup, credential read, DNS, connect, TLS, send, read and parsing. On timeout or interrupt it kills and reaps that child; no replacement request. A process-local 10-second alarm also bounds the request work. Reaping is observed after stopping, not asserted to have happened at the deadline. |
| Response | At most 256,000 decrypted HTTP bytes consumed, including status line, headers and transfer framing. No read crosses the remaining budget. Reaching the cap is a failure, even at exact fit; no extra sentinel byte. TLS/network overhead and already received kernel/TLS buffering are not a wire-traffic cap. |
| Framing | Requests `Connection: close`, waits for EOF within the same deadline, then uses Python's HTTP parser on bounded memory. A peer that leaves the connection open times out even if it has sent a complete body. No decompression; encoded responses fail. |
| Validation | HTTP 200 and JSON media type required. Application code must be integer zero, status `success` or `succeed`, error and warning fields empty. Any warning stops, including quota/account warnings accompanying success. Results count must match, at most 10; IDs must be distinct decimal strings. Duplicate JSON keys fail. |
| Output | One ASCII JSON report, at most 100,000 bytes including newline. No title, artist, tag, raw response, query URL, exception text or response header. Numeric identities containing the application ID are omitted. No raw artifact, credentials or audio retained. |
| Credential | Only the isolated opt-in child opens the supplied newly named file. Regular file, no symlink/FIFO, at most 160 bytes; ASCII application ID, 6–128 letters/digits/underscore/hyphen. No environment-file reader and no ID in arguments. |
| TLS/privacy | Normal certificate and hostname verification. TLS key logging is never enabled by `SSLKEYLOGFILE`. Child core-file limit is zero. Child stderr is discarded; failures have fixed local names. |

The [response-code reference](https://developer.jamendo.com/v3.0/response-codes)
documents code 5 (invalid ID), 6 (rate limit) and 11 (suspended application).
Every nonzero code stops this runner. Its response-code prose uses `succeed`,
while the tracks example uses `success`; both are accepted with all other checks.
There is no automatic retry, including after timeout, empty results or refusal.

The implementation uses maintained Python 3.14
[socket/TLS](https://docs.python.org/3.14/library/ssl.html),
[HTTP parsing](https://docs.python.org/3.14/library/http.client.html) and
[process supervision](https://docs.python.org/3.14/library/subprocess.html).
The small raw receive loop makes the entire HTTP-byte ceiling explicit and avoids
hidden retry behavior in a higher-level client. It is evaluator-only code, not a
new production transport framework.

## Operator walkthrough: chosen API route only

1. Read the reviewed PR/head and this request's limits with the coordinator.
   Confirm this API check is the route to test; a previous working direct route
   can be investigated separately without claiming it is impossible.
2. If an operator-owned API ID already exists, use it privately. If the operator
   chooses to create one, the official [developer portal](https://devportal.jamendo.com/)
   links account signup and application creation. Describe the non-commercial
   SobaFM bot and link [this repository](https://github.com/slackysoba/sobafm).
   The documented default is read-only. This check needs no read-and-write
   upgrade, OAuth token, client secret, purchase or paid plan. If the actual flow
   asks for review or payment, stop and report that fact rather than assume it.
   Account creation is the operator's action, not this runner's.
3. In **Notepad**, create a new private plain-text file outside the repository,
   for example `jamendo-metadata-id.txt`, containing only that application ID.
   Save as UTF-8 without a BOM. Do not paste it into chat, a terminal command,
   GitHub or an existing Discord/Gemini environment file. Give the coordinator
   only its private path. The agent must never inspect its contents. Use the
   corresponding Linux-visible absolute path when invoking the check.
4. The coordinator records the maintainer's fresh go-ahead in their own words,
   tied to the reviewed source commit. This step is still pending. The proposed
   wording below authorizes only one invocation; it grants no retry or next step.
5. Use the existing locked Linux environment for that reviewed commit. No live
   invocation installs or synchronizes packages. The default demonstration is:

   ```sh
   .venv/bin/python -I -B evaluations/jamendo_access/probe.py
   ```

   It prints `disabled`. After the separate go-ahead, the exact command shape is:

   ```sh
   .venv/bin/python -I -B evaluations/jamendo_access/probe.py \
     --send-to-jamendo /absolute/private/jamendo-metadata-id.txt
   ```

   Replace only the private file path, never put the ID itself in the command.
   Do not run under shell tracing, Python tracing/debugging or a generic wrapper
   that records local variables. Do not invoke the internal child/network
   functions directly: the CLI supervisor owns the hard deadline and reaping.
6. Keep only the sanitized report and exit status, bound to the commit. Exit 0
   with `metadata_received` establishes only a validated metadata response; zero
   results is still possible. Exit 1 means stop and discuss the fixed reason.
   Neither outcome permits another request. Do not paste the ID file or a raw
   provider response into evidence. A failure does not justify header changes,
   another shared ID, a proxy, a browser workaround or an automatic rerun.

### Proposed first live go-ahead

> I authorize one invocation of the reviewed #173 Jamendo metadata evaluator at
> the source commit identified by the coordinator, using my privately supplied
> operator-owned application-ID file. Allow at most one HTTPS tracks GET, ten
> results, 256,000 decrypted HTTP response bytes and ten seconds, with no retries,
> redirects, audio, model or Discord calls. Keep only the sanitized report, at
> most 100 KB. Stop on any refusal, warning, authentication/rate-limit error,
> malformed response or bound. This does not authorize registration, spending,
> another request, source-policy changes, admission or production integration.

This is a proposal to present after review, **not permission already received**.

## Offline verification

Use an idle same-lock Linux environment, following the project's storage and
ownership rules. These tests use synthetic IDs/files, fakes and local peers only:

```sh
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
uv run pyright evaluations/jamendo_access
uv run pytest -q evaluations/jamendo_access
```

The evaluator checks cover default-off behavior, invalid/suspended IDs, fixed
request contents, redirects/refusals, a dropped connection without retry,
address failure without fallback, response bounds including headers, application
status/code/warning/count validation, redaction, timers, socket closure and
supervisor kill/reap. The loopback transport tests replace TLS with a plain local
socket; a separate check verifies real TLS context certificate/hostname settings
and disabled key logging. These tests do not establish actual Jamendo TLS,
account, metadata or stream behavior. No live result is included in this change.
