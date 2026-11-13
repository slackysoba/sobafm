# Empty-refresh metadata comparison

Prepared for [#213](https://github.com/slackysoba/sobafm/issues/213). This is a
reviewable protocol, not execution evidence. Run only after independent review
and the coordinator's assignment of the exact source and invocation under the
[standing necessary-test authorization](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6798078261).
The preparatory PR does not close #213. No new source call was made to prepare it.

## Diagnosis and question

The [reviewed a8 result](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6843485375)
selected numeric track ID `999986` from ten listing rows, then received a valid
HTTP 200/application-code-zero response with zero rows on immediate by-ID
refresh. It made two metadata requests and no audio request. Inspection of the
recorded sanitized report and `probe.py` found no demonstrated local defect:
the selected decimal ID is URL-encoded into the tracks endpoint, listing-only
filters are removed, and count/container validation precedes `refresh_empty`.
Neither removal nor authentication failure nor an API defect is established.

The [official tracks reference](https://developer.jamendo.com/v3.0/tracks), read
on 2026-11-12 (maintainer timezone), documents ID lookup and the union
`type=single albumtrack`. It describes `audioformat` as selecting the returned
stream format, with mp31 as the default and mp32 as VBR. The
[response-code reference](https://developer.jamendo.com/v3.0/response-codes)
distinguishes code zero from invalid-client, rate-limit and suspended-app errors.
These docs do not explain an empty successful lookup.

The smallest useful contrast here is the original fixed-ID refresh versus the
same request without `audioformat`. This tests an **unproven format-associated
response difference**, not an asserted bug. Keeping both types avoids confounding
the comparison with the documented album-only default. Keeping the historical
ID avoids another listing/first-candidate attempt. No cache-busting field,
alternate account, endpoint or undocumented request spelling is introduced.

## Exact finite invocation

Use the existing Linux CPython 3.14 runtime and dedicated Client ID file. The
coordinator must bind the reviewed/merged commit, evaluator blobs, interpreter,
clean execution directory and fresh evidence destination before launch. The
following is a command template: replace the placeholder **path**, never the
credential value. There is no track ID or format override.

```sh
python -I -B evaluations/jamendo_stream/probe.py \
  --diagnose-empty-refresh /operator/private/client-id.txt
```

Run exactly once, with the existing supervised PID/wait/exit bookkeeping. Do not
invoke the stream flag `--send-to-jamendo`. Default invocation still performs no
work. The coordinator records the concrete private launch text and public source
binding; private paths and credential values must not enter public records.

1. GET `https://api.jamendo.com/v3.0/tracks/` with these fields in order:
   `client_id` from the runtime-only file, `format=json`,
   `type=single albumtrack`, `audioformat=mp32`, `id=999986`, `limit=1`.
   This is the a8 refresh shape and query order, using the same URL encoder.
2. Only if step 1 is a valid successful **empty** result, send one GET to the
   same endpoint with exactly those fields except `audioformat`, preserving the
   remaining order. No explicit mp31 value is sent.
3. Stop. A matching row at step 1 ends the invocation after one call. Any
   transport/framing/JSON/application/count/identity failure stops immediately;
   only the valid empty step-1 observation enables step 2. There is no listing,
   alternate track, repetition, automatic retry, redirect, audio request, decoder,
   model, Discord, paid fallback, browser impersonation or download request.

Both steps use existing strict metadata framing and envelope validation. A
nonempty result must have exactly one row whose string ID is exactly `999986`.
This diagnostic deliberately makes **no admission decision** and does not parse
or use the returned audio field. `metadata_validated` stays false; selected track
and legacy search/refresh/stream counters stay unset/zero. Production gates and
the original stream mode's full refresh binding remain unchanged.

## Bounds, report and cleanup

| Boundary | Limit and observation |
| --- | --- |
| Calls | At most two sequential metadata connections/GETs. Existing first-address-only TCP/TLS behavior; no retries or redirects. |
| Bytes | At most 256,000 consumed decrypted HTTP bytes per response, including headers; at most 512,000 total. Reads never cross the cap; exact-cap responses fail. Zero audio bytes. These are not wire/TLS-buffer/kernel totals. |
| Memory | Existing 16,384-byte header and 8,192-byte read limits. Each metadata body is bounded; raw response bytes are discarded after parsing and no response is persisted. |
| Time | Existing 117-second supervisor work deadline including startup, credential read, DNS, TLS, both requests and parsing; independent child alarm is never reset between steps. |
| Cleanup | Existing original three-second cleanup deadline and retained child owner. Timeout/interrupt kills and reaps the sole child. Late reaping retains a failed verdict and may exceed the proposed 120-second total; no additional request or abandonment follows. |
| Credential | Existing child-only bounded regular-file read and Client ID validation; minimal environment, discarded stderr and disabled core files. Agents inspect only existence/type/size. No Client Secret. |
| Evidence | One sanitized ASCII JSON report up to 100 KB; all invocation artifacts at most 100 KB and total #213 evidence at most 5 MB, within the standing aggregate 500 MB logs/exports ceiling. No raw body, URL, marker, title, artist, credential, audio or exception text. |

The report retains existing fields and adds `diagnostic_steps` (at most two), each
with a fixed `variant` (`mp32` or `default`), `transfer` using existing numeric
HTTP/byte/EOF counters, `results` using existing safe application/count/type
diagnostics, and `identity` (`not_evaluated`, `match` or `mismatch`). No returned
ID is copied into the report. `refresh_diagnosis` is null until a complete
diagnostic result, then one of the following fixed labels. The existing
credential-substring suppression and report cap apply to the whole child report.

| Diagnosis | Meaning and next decision |
| --- | --- |
| `mp32_found` | The original shape returned the historical ID now. The a8 emptiness was not reproduced; its historical cause remains unknown. No second request. |
| `default_only_found` | The mp32 request was empty and the immediately following omitted-format request returned the ID. This is a format-associated observation, confounded by time/server variation, not proof of causal format filtering. No mp31 playback fallback is approved. |
| `both_empty` | Both shapes returned valid empty lists. The format contrast did not distinguish the cause; removal, stale listing/index, endpoint behavior and account behavior remain unproved. |

`outcome=metadata_diagnosed` / exit 0 means the finite comparison completed and
cleanup succeeded, including when both lists were empty. It does **not** mean
source compatibility. Errors remain `inconclusive` / exit 1 with the fixed local
reason and partial step facts. Parent interruption can leave partial call/byte
accounting unknown, as before; do not invent zeroes for an absent child report.

Retain the launch/exit/source/runtime receipts and all failed attempts, including
a7's unknown historical accounting and cleanup. After the invocation, observe
reaping and source-bound process ownership without reading process environments.
Submit the actual sanitized result for independent review. Do not repeat the
experiment or proceed to audio based on any label. Any later audio protocol is
separate, must restore every refresh/admission gate, and remains within the
existing 20 MB discard-only ceiling. This comparison does not establish audio
access, rights, integrity, completeness, quality, production capacity or release
readiness.

## Offline verification

The explicit evaluator suite remains outside ordinary CI collection. Run it with
the required locked synchronization, all hooks and full ordinary suite, plus:

```sh
uv run pytest -q evaluations/jamendo_stream
uv run pyright --pythonversion 3.14 --pythonplatform Linux --level error \
  evaluations/jamendo_stream/probe.py evaluations/jamendo_stream/test_empty_refresh.py
```

Regression cases cover all three outcomes, exact query-field differences, no
audio path, early stop on malformed/error responses, response caps, one original
alarm across both calls, sanitized reports, real loopback redirect refusal and
supervised invalid synthetic input. Existing transport and original-clock
cleanup cases remain applicable. All credentials and response bytes in these
tests are synthetic; none authorizes or performs a live source request.
