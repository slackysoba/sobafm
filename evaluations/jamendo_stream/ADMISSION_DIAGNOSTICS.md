# Single-listing production admission diagnosis

Prepared for [#229](https://github.com/slackysoba/sobafm/issues/229), based on
`0930c978160dc38630e8b53ac60fb866ff9f9c79`. The independently reviewed
[a14 result](https://github.com/slackysoba/sobafm/issues/228#issuecomment-6851368313)
passed the production envelope but refused all ten rows as `unsupported_evidence`.
Its first recording boundary is unknown. The separate
[static triage](https://github.com/slackysoba/sobafm/issues/228#issuecomment-6851353549)
identified six possible boundaries and a documentation/allowlist discrepancy for
`content_id_free`; it did not establish that the field appeared in a14 or caused
any refusal. This diagnostic does not change that historical evidence or admit
the named field. [#230](https://github.com/slackysoba/sobafm/issues/230) owns
independent documentation research and is not an unmerged code dependency.

Preparation and regression verification are offline. Independent implementation
and protocol review and merge precede one separately assigned invocation under
the standing necessary-test authorization. This implementation assignment grants
no live execution, credential read, provider/Discord request, build or native
execution. The issue remains open for the separately reviewed diagnostic result.

## Prospective invocation and binding

Bind the reviewed merged source commit, clean execution checkout, fresh evidence
destination and existing Linux CPython 3.14 interpreter. Privately record exact
interpreter version/executable and source blob, byte length and SHA-256 bindings.
These describe local source/runtime, never response fingerprints. Retain launch,
PID/wait/exit and cleanup receipts without tracing or environment dumps.

```sh
python -I -B evaluations/jamendo_stream/probe.py \
  --diagnose-admission /operator/private/client-id.txt
```

Only the supervised child reads the existing dedicated Client ID file; agents
inspect file metadata only. Default invocation remains disabled. No other mode
or internal-function invocation substitutes for this protocol. No environment,
dependency or image installation is needed.

The isolated child loads the checkout's `src`, not an older installed package.
Bind `probe.py` and its complete local production import closure:
`src/sobafm/__init__.py`, `catalog.py`, `credits.py`, `jamendo_admission.py` and
`jamendo_catalog.py`. The isolated offline test verifies all five origins without
site-packages. Production source changes in this implementation; historical image
qualification remains bound to its original source and is not transferred.

At most one HTTPS GET/connection to `api.jamendo.com:443`, `/v3.0/tracks/`, using
the existing production `Request`/`_COMMON` listing builder and ordered parameters:

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

This is the same finite diagnostic subset, not production musical search. Receive
the complete bounded response, then call production `_rows(data, 10, diagnostic=...)`
once. The legacy `metadata_rows` pre-parser is not called. An envelope refusal
stops immediately with the existing safe reason and envelope enum.

For each returned row in order, call `parse_listing` exactly once with the complete
row and a fresh `AdmissionDiagnostic`. On a returned listing, call `evaluate` with
the locally enabled policy, current UTC observation time and `RANKING` purpose.
Record only ordinal, the existing reason and the two optional fixed diagnostic
enums below. Catch the existing `AdmissionInputError` without retrying, filtering
fields or continuing validation of that refused row. Continue to the next row,
up to ten. Never retain the source row or a decision in the report.

**Stop unconditionally before selection, refresh or audio, even if a row passes.**
No candidate is constructed and no whole-row comparison/fingerprint is computed.
Completed diagnosis reports `admission_diagnosed`, exit 0 only if cleanup succeeds;
this means the bounded observation completed, even if every row refused or the
listing was empty. It does not mean metadata qualification succeeded.
`metadata_validated` stays false; selected identity and stream hash remain null;
refresh/audio counters stay zero; comparison and decision-match fields stay null.
An empty listing produces no row observations and establishes no refusal cause.
Transport/envelope failures, interruption, timeout and failed cleanup remain
`inconclusive`, exit 1. A partial observation list after interruption is incomplete.

No retry, pagination, redirect, alternate request/address, filtered retry,
selection, refresh, audio, decoder, model, Discord, browser impersonation, format
substitution or paid fallback occurs on any outcome.

## Exact observation semantics

`AdmissionDiagnostic` resets at the start of each production parse. Normal callers
omit it. The shared `object_fields` acceptance and the parser's order, reasons,
allowlists, bounds, rights/notice checks, returned listings and evidence remain
unchanged. The observer annotates the existing helper's four caller boundaries;
it never implements a parallel parser or determines acceptance.

| `boundary` | First encountered unsupported check |
| --- | --- |
| `row_fields` | Complete row has unsupported keys, after object/string-key and required-field checks |
| `musicinfo_fields` | Supplied music information has keys outside its existing allowlist |
| `tag_groups` | Supplied tag groups have keys outside genres/instruments/vartags |
| `license_fields` | Supplied license flags have keys outside their existing allowlist |
| `details_bound` | Flattened details exceed 24 entries or a detail exceeds 160 characters; one combined label |
| `recording_type` | Type is outside empty/single/albumtrack/track while the prior reason is still provisional |

Earlier non-unsupported failures leave `boundary` null. Music-information values
are visited in existing insertion order; an invalid value can precede an unknown
tag group. Excluded-form or rights reasons precede and suppress the recording-type
branch. That branch does not return early: the existing later credit rendering
may still refuse. In that case `boundary=recording_type` records the encountered
unsupported branch, while `reason` retains the final credit refusal. Neither
field implies unvisited checks passed or identifies any later failures.

Only at `row_fields`, `unknown_row_keys` describes the unsupported key set:

| Enum | Meaning |
| --- | --- |
| `documented_only` | Exactly the documented `content_id_free` name |
| `documented_and_other` | That name plus at least one other unsupported name |
| `other_only` | Unsupported names excluding that documented name |

All other boundaries leave this field null. Comparison inspects keys only after
the original helper refuses; it never reads the named field's value. These enum
labels are not supplied names and make no assertion about the flag's semantics,
rights, completeness or truth. No field is ignored, approved or newly accepted.

The new report entries contain only fixed `ordinal`, `reason`, `boundary` and
`unknown_row_keys` fields. No actual unknown keys, values, paths, key/detail
lengths/counts, source fingerprints, exception text, credits, URLs, identities or
credentials are emitted. Existing HTTP transfer accounting (consumed byte counts,
status and framing) remains; envelope count/type fields stay null. Credential
substring suppression, ASCII JSON and the existing output ceiling still apply.

## Bounds and ownership

| Boundary | Unchanged limit |
| --- | --- |
| Requests | One listing GET/connection, at most ten rows; zero refresh/audio |
| Receipt | At most 256,000 consumed decrypted HTTP bytes total, including status/headers/body; exact cap refuses without sentinel read; not wire/TLS-buffer/kernel totals |
| Buffers | 16,384-byte header and 8,192-byte read bounds; no raw-response artifact |
| Work | Original 117-second supervisor deadline and child alarm, including imports, credential read, DNS/TLS, request and all parsing; never reset |
| Cleanup | Original three seconds, same sole-child owner through observed reaping; late release stays failed and can exceed 120 seconds total |
| Output | Existing 100,000-byte ASCII JSON report ceiling; full ten-row synthetic report below 4 KB; fresh invocation evidence at most 100 KB and total issue evidence at most 5 MB within the standing 500 MB logs/exports ceiling |

Retain the owner and failed verdict through repeated interruption and actual
reaping. Missing child output leaves request accounting unknown within the caps,
never zero by inference. Observe source-bound process ownership without reading
environments, preserve original outcomes/artifacts and stop for independent result
review. No result authorizes another request or establishes source compatibility,
rights truth, audio access, completeness, listening quality, containment or release
readiness.

## Offline verification

Ordinary regressions belong to `tests/test_jamendo_catalog.py`. Opt-in regressions
are `evaluations/jamendo_stream/test_admission_diagnostics.py`; all evaluator tests
remain outside ordinary CI. Run locked sync, all hooks and full ordinary pytest,
plus:

```sh
uv run pytest -q evaluations/jamendo_stream
uv run pyright --pythonversion 3.14 --pythonplatform Linux --level error \
  evaluations/jamendo_stream
```

Synthetic cases cover all six labels/three key classifications, exact limits,
first-failure ordering, unchanged reasons/listings/decisions/evidence/credits,
hostile-key/value redaction and unread values, complete rows parsed once, exact
request, zero selection/refresh/audio even on passing rows, envelope refusal,
isolated source binding, the original byte/alarm limits, interruption and retained
late-reaping failure. These checks do not identify a14's actual cause. Required
author checks, the exact pushed-commit helper, CI and independent review precede
squash merge; the separately assigned diagnostic remains outstanding after merge.
