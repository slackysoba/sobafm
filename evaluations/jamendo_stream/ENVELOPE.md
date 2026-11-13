# Single-listing production envelope diagnosis

Prepared for [#224](https://github.com/slackysoba/sobafm/issues/224) from
`afc2156977d946e51995975b2c1a2d0d3d2e75c7`, following the independent
[a12 result review](https://github.com/slackysoba/sobafm/issues/222#issuecomment-6849181182).
A12 refused before candidate admission; its actual failed condition is unknown.
The original diagnostic changed no acceptance policy. The independently reviewed
[a13 result](https://github.com/slackysoba/sobafm/issues/224#issuecomment-6850607683)
at `48d044320aaf7099e961994028c96370b53e9e7a` remains an inconclusive
`malformed_response` / `unexpected_header_keys`, exit 1. It established neither
the extra key, provider application success nor actual row count.

[#226](https://github.com/slackysoba/sobafm/issues/226) now permits bounded additive
JSON header metadata under the documented [envelope boundary](../../docs/jamendo-catalog.md#validation-and-evidence).
Required header checks, whole-response bounds and closed root/recording/notice
validation remain. This diagnostic observes the current production parser;
`unexpected_header_keys` is historical only and is no longer emitted. No old
failure becomes a pass, and this change does not predict a later result or
authorize a retry.

Preparation and synthetic verification are offline. Independent implementation
and protocol review and merge precede one separately assigned invocation under
the standing necessary-test authorization. No actual credential reader, source
request, native/image execution or build is assigned to this preparation.

## Exact prospective invocation and bindings

Bind the reviewed merged commit, clean execution checkout, fresh evidence
destination and existing Linux CPython 3.14 interpreter. Record the exact
interpreter executable/version and source blob/length/SHA-256 bindings privately;
these are local-source/runtime bindings, never response fingerprints. Retain
launch, PID/wait/exit and cleanup receipts. Use the existing sole-child supervisor,
without tracing, environment dumps or argument capture containing credentials:

```sh
python -I -B evaluations/jamendo_stream/probe.py \
  --diagnose-envelope /operator/private/client-id.txt
```

Only the supervised child reads the existing dedicated Client ID file; agents
inspect its existence/size only. Default invocation remains disabled. No other
flag or direct internal-function invocation substitutes for this protocol.
No environment, dependency or image installation is needed for execution.

The isolated child loads the checkout's `src` before production imports, so it
does not use an older installed package. Bind `probe.py` and the complete pure
local import closure: `sobafm/__init__.py`, `catalog.py`, `credits.py`,
`jamendo_admission.py` and `jamendo_catalog.py`. The isolated offline test verifies
all five origins without site-packages. This patch changes production source;
a later application build must account for it. The existing `fed87387` image and
its qualified component evidence retain their exact bindings; do not relabel them.

Exactly one HTTPS listing GET at most, to `api.jamendo.com:443`, `/v3.0/tracks/`,
with these ordered parameters:

| Parameter | Fixed value |
| --- | --- |
| `client_id` | Runtime-only dedicated ID |
| `format` | `json` |
| `type` | `single albumtrack` |
| `audioformat` | `mp32` |
| `include` | `musicinfo licenses` |
| `limit` | `10` |
| `order` | `id_desc` |
| `ccnc` | `true` |
| `durationbetween` | `1_480` |

The builder reuses production `Request`/`_COMMON` and the reviewed qualification
listing builder unchanged. This remains a fixed diagnostic subset, not production
musical search. Receive the complete bounded envelope, then call production
`_rows(data, 10, diagnostic=...)` once. The older evaluator `metadata_rows` parser
is deliberately not called in this mode: it cannot preempt the production refusal
or create a second policy engine. Nothing strips unknown fields or row contents.

**Stop unconditionally before candidate admission, refresh or audio.** A passing
envelope reports `envelope_passed`, null `envelope_condition`, exit 0 only if
cleanup succeeds. Even empty rows or rows that would fail candidate admission
can pass this envelope-only test. `metadata_validated` remains false, selected
identity and stream hash null, candidate refusals empty, refresh/audio counters
zero and comparison/decision-match fields null. These are unexecuted boundaries,
not successful admissions. Refusal reports `inconclusive`, the unchanged public
`Failure` label, the fixed first-condition enum, and exit 1. Transport failure or
an interruption before parser completion leaves the condition null.

No retry, pagination, redirect, alternate request/address, refresh, candidate
selection, audio, decoder, model, Discord, browser impersonation, format
substitution or paid fallback occurs, regardless of outcome.

## First refusal and safe output

`EnvelopeDiagnostic` observes the existing `_rows` result; normal callers pass no
observer and keep the same `Refusal`/tuple return. It resets on each call. No
diagnostic enters the product user flow. The labels and existing order are:

| Stage | Fixed labels and conditions |
| --- | --- |
| Payload/JSON | `payload_size` above 1,048,576 production bytes; `json` for UTF-8/JSON, duplicate-key, constant or parser recursion refusal. These earlier failures do not imply a structural diagnosis. The prospective transport cap is smaller. |
| Recursive structure | `depth` above 8; `object_members` above 64, then `key_length` above 64 characters; `list_length` above 100; `string_length` above 32,768 characters |
| Envelope objects | `root_shape` or `header_shape` for wrong type/missing required keys; `unexpected_root_keys` for unsupported root fields. Bounded extra header members alone do not refuse. |
| Provider | `provider` for the existing status/code failure; `warning` for nonempty warnings/errors |
| Rows | `results_shape` for non-list results; `results_count` for more than ten rows or invalid/mismatched declared count |
| Optional total | `total_type` for non-integer (including bool); `total_value` for a value below the returned row count |

Structure is visited before envelope field validation, depth first in JSON object
insertion/list order; object member count precedes key length, and missing required
root keys precedes unknown root keys. The first failing condition stops traversal. Exact
limits pass this structural stage; a later gate may still refuse. Unknown row
fields are still structurally visited; the envelope never grants row admission.
The original `_pairs`, `_constant` and recursive bounds remain authoritative.
The shared `object_fields` helper remains strict for root and recording evidence;
the header separately requires an object containing every mandatory member.
Extra header data is never reported or used for admission.

The diagnostic retains only a fixed enum label, never source-controlled field
names, paths, values, field lengths, raw text or response fingerprints. The
existing report retains bounded HTTP transfer accounting (status, consumed byte
counts and framing), but envelope count/type fields stay null in this mode.
No source rows, credits, URLs, identities or credentials are serialized.
Existing credential-substring suppression and ASCII JSON output limits apply.

## Finite ownership and evidence limits

| Boundary | Limit |
| --- | --- |
| Requests | One listing GET/connection, at most ten rows; zero refresh/audio |
| Receipt | At most 256,000 consumed decrypted HTTP bytes total, including status/headers/body; exact cap refuses without a sentinel read; not wire/TLS-buffer/kernel totals |
| Buffers | Existing 16,384-byte header and 8,192-byte read bounds; no raw-response artifact |
| Work | Original 117-second supervisor deadline and child alarm, including imports, credential read, DNS/TLS, request and parser; never reset |
| Cleanup | Original three seconds, same sole-child owner through observed reaping; late release remains failed and may exceed 120 seconds total |
| Output | Existing 100,000-byte ASCII JSON report ceiling; this mode's fixed schema is below 4 KB; all fresh execution evidence at most 100 KB and total #224 evidence at most 5 MB within the standing 500 MB logs/exports ceiling |

A timeout, interruption or failed cleanup remains inconclusive, exit 1. Repeated
interruption does not release the child owner; the supervisor keeps waiting after
the original cleanup deadline and preserves its failed verdict. A missing child
report leaves actual request accounting unknown within the caps, never zero by
inference. Keep original failure and retained debt until observed release.
Check source-bound process ownership without reading environments, preserve prior
artifacts unchanged, then stop for independent result review. No result authorizes
another invocation or establishes source cause beyond its first condition, source
compatibility, rights truth, audio access, completeness, quality, containment or
release readiness.

## Offline verification

Run locked sync, all hooks and the full ordinary pytest suite, plus:

```sh
uv run pytest -q evaluations/jamendo_stream
uv run pyright --pythonversion 3.14 --pythonplatform Linux --level error \
  evaluations/jamendo_stream
```

Synthetic cases exercise exact recursive boundaries including header additions,
accepted scalar/object header metadata, missing/invalid required fields, unknown root keys,
invalid optional totals, earlier parser failures, deterministic first refusal,
unchanged public outcomes, one exact request, safe output, no admission even on
envelope success, isolated import binding, original alarm, byte caps and retained
late-reaping failure. The evaluator suite remains opt-in and outside ordinary CI.
Offline evidence does not identify what happened in a12 or a13's unknown key.
