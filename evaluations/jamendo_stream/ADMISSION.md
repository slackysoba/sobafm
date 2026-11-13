# Metadata qualification using production admission

Prepared for [#222](https://github.com/slackysoba/sobafm/issues/222) from
`0a4f76aee2a1edab3a39918c661a4f3ff918699c`, following the independently confirmed
[static contract discrepancy](https://github.com/slackysoba/sobafm/issues/219#issuecomment-6848158072).
The historical access probe rejects every normalized row change; accepted
[ADR 0013](../../docs/decisions/0013-jamendo-discovery-streaming.md) instead allows
a fresh valid decision and immutable credits while refusing changed material
content. This new mode applies that existing production contract. It does not
change production policy, the older modes, or any historical result.

The prospective [#235](https://github.com/slackysoba/sobafm/issues/235) revision uses
the accepted production musical-search builder with a fixed trusted jazz query.
It tests request construction and metadata admission, not Gemini interpretation
or ranking. Production source, policy and diagnostic modes remain unchanged.

Preparation is offline. Independent implementation/protocol review and merge
precede a separately assigned invocation under the standing necessary-test
authorization. No invocation belongs to this implementation. The earlier a11
empty listing and a15/a16 empty diagnostic-subset listings remain inconclusive;
their causes and a14's refusal cause remain unknown. This change cannot guarantee
a nonempty response, identify a prior changed field or establish live content-ID
compatibility. Historical results and their source bindings remain unchanged.

## Exact prospective invocation

Bind the reviewed merged commit, clean execution checkout, evaluator and imported
production source blobs, existing Linux CPython 3.14 interpreter and fresh evidence
destination. Only the supervised child reads the existing dedicated Client ID
file; agents inspect file metadata only. Substitute only a private path:

```sh
python -I -B evaluations/jamendo_stream/probe.py \
  --qualify-metadata /operator/private/client-id.txt
```

Use existing supervised PID/wait/exit bookkeeping without tracing or variable
capture. Default invocation remains disabled. Do not substitute an older flag or
call internal functions. No environment, image or dependency installation is
required: this mode loads the checkout's `src` before importing the standard-library
production admission/catalog/credits modules. It does not use a potentially stale
installed application. Bind that source and its transitive local imports as well
as `probe.py`; production modules remain unchanged by this PR.

At most two sequential HTTPS GETs to `api.jamendo.com:443`, `/v3.0/tracks/`:

| Request | Exact ordered query parameters |
| --- | --- |
| Listing | runtime-only `client_id`, `format=json`, `type=single albumtrack`, `audioformat=mp32`, `include=musicinfo licenses`, `search=jazz`, `limit=10`, `offset=0` |
| Refresh | runtime-only `client_id`, `format=json`, `type=single albumtrack`, `audioformat=mp32`, `include=musicinfo licenses`, `id=<first admitted ID>`, `limit=1` |

The listing constructs trusted fixed `Query(search="jazz")`, obtains the first
`SearchWindow.request()` at offset zero with the locally enabled policy, and
propagates any refusal before transport. It copies that production request with
only `limit` reduced from 100 to 10. It neither builds a parallel parameter list
nor requests another page. The copied request is never passed to
`SearchWindow.receive`, which requires its original request object. Complete-row
admission below remains authoritative. The refresh uses `refresh_request`
unchanged. Both requests include **`include=musicinfo licenses`**.

This is the already accepted production musical request, not a source-access
workaround or a relaxation of license/notice checks. It replaces the qualification
mode's diagnostic-only order, CC and duration selectors; no production filter or
permission changes. Envelope and admission-diagnostic modes retain their historical
requests, including those selectors. Metadata transport still uses the existing
bounded TLS/HTTP exchange, not production's larger transport limits.

1. Receive one listing with at most ten rows. Keep fixed envelope/count diagnostics
   and use production's `_rows` envelope parser without dropping fields. Reuse its
   internal parser to enforce the evaluator's ten-row bound independently of the
   production window's 100-row page. The narrow private imports for this parser
   and historical diagnostic requests keep one implementation and are covered
   by explicit evaluator typing and regression checks.
2. In returned order, pass each complete row to `parse_listing`, then `evaluate`
   with the locally enabled policy, current UTC observation time and `RANKING`
   purpose. Record only ordinal and fixed reason for refused rows. Select the
   first admitted result; stop if none. Do not apply the historical evaluator's
   separate download-flag, duration, backlink or narrower stream-host gates.
   Production normalization and all its supported-evidence checks are authoritative.
3. Immediately request that ID once with `refresh_request`. Pass the complete
   response and immutable selected candidate to `refresh`. Its fresh decision
   has `REFRESH` purpose and new observation time. It rejects identity/material
   content changes, missing recordings, invalid routes, unsupported/malformed
   evidence and unreadable/conflicting rights or notices. Unknown fields are
   never removed to obtain a pass. A failed refresh never chooses another row.
4. On success, retain only fixed structural comparisons and the boolean
   `previous_decision_matches`. A false value alone is **not** a refusal: valid
   changed notices/licenses can produce new immutable credits while the prior
   candidate remains unchanged. Locator/incidental changes do not invalidate
   admission when production considers them irrelevant.
5. **Stop unconditionally before audio.** Success reports `metadata_admitted`,
   `metadata_validated=true`, exit 0 only when cleanup also succeeds. This is
   metadata admission under the provisional rule, never streaming readiness.
   Refusals report a fixed reason, `inconclusive`, exit 1. Production reasons come
   only from `Reason`/`Failure`; transport and envelope diagnostics remain fixed.

No retries, pagination, redirects, alternate candidate after selection, address
fallback, audio connection, decoder, model, Discord, download, browser
impersonation, format substitution or paid fallback occurs.

## Structural diagnostics and redaction

The eight existing [structural categories](METADATA_CHANGES.md#fixed-categories-and-normalization)
remain diagnostic only. In this mode they compare two fully admitted whole rows,
including incidental fields, without discarding keys or nested values. Object key
order and opaque audio-query pair ordering are normalized; the full locator is
included so a changed valid host is observable as `stream_route`. Unlike the
historical mode, string duration is not coerced: production refuses it. Production
may normalize an HTTP/HTTPS license or integer/string identity equivalently while
the structural category changes. The category cannot invalidate or approve a
decision. A refused refresh has no comparison and leaves `evidence_changes=null`.

Reports contain only the eight fixed category keys with `changed`/`unchanged`,
fixed production refusal reasons with row ordinals, the decision-match boolean,
and existing safe transfer/count observations. They contain no source-controlled
names, fields, values, credits, URLs, identifiers, fingerprints or credentials.
`selected_track_id` is always null; stream counters remain zero and its hash null.
Transient row/category digests and fresh credit snapshots are never serialized.
Credential-substring suppression and the report-size limit remain in force.

## Bounds and retained ownership

| Boundary | Unchanged limit |
| --- | --- |
| Requests | One listing of at most ten candidates and one refresh: at most two metadata GETs/connections, always zero audio |
| Bytes | 256,000 consumed decrypted HTTP bytes per response including status/headers/body; 512,000 aggregate. Exact-cap responses refuse without sentinel reads. Not wire/TLS-buffer/kernel totals |
| Buffers | 16,384-byte header and 8,192-byte read bounds; transient metadata only |
| Work | One 117-second supervisor deadline and child alarm, including imports, credential read, DNS/TLS, both requests and validation; no reset |
| Cleanup | Original three-second cutoff and sole-child owner retained through observed reaping; late release stays failed and may exceed 120 seconds total |
| Artifacts | Sanitized ASCII JSON report; all artifacts for an invocation at most 100 KB, total #222 evidence at most 5 MB within the standing 500 MB logs/exports ceiling |

Keep source/runtime/launch/exit receipts and sanitized report, observe reaping and
source-bound process owners without reading process environments, then stop for
independent result review. A missing child report leaves request accounting
unknown within the caps. Never infer zero requests from missing output. No result
authorizes another request or establishes provider cause, source compatibility,
rights truth, audio access, completeness, quality, containment or release readiness.
Preserve prior outcomes and artifacts unchanged.

## Offline verification

Run locked sync, all pre-commit hooks and full ordinary pytest, plus:

```sh
uv run pytest -q evaluations/jamendo_stream
uv run pyright --pythonversion 3.14 --pythonplatform Linux --level error \
  evaluations/jamendo_stream
```

Synthetic regressions exercise the real production parse/evaluate/refresh boundaries:
fresh immutable credits, allowed incidental changes, normalization equivalence,
material changes, conflicting rights/notices, unsupported evidence, malformed input,
invalid routes, unreadable credits, empty results, first eligible selection, exact
production builder reuse, exact fixed request parameters, ten-row cap, builder
refusal before transport, safe output, original alarm, byte caps, interruption and retained
late-reaping failure. Success and failure paths both assert zero audio. Existing
historical-mode, redirect, transport and ownership regressions remain applicable.
Evaluator tests remain outside ordinary CI and make no source/provider calls.
