# Fixed-category listing and refresh comparison

Prepared for [#219](https://github.com/slackysoba/sobafm/issues/219), following the
[independently reviewed a10 result](https://github.com/slackysoba/sobafm/issues/213#issuecomment-6846810339).
a10 passed the selected identity and candidate checks, then refused normalized
whole-row inequality before audio. No raw rows, fingerprints or field differences
were retained. Its differing field and cause cannot be reconstructed. This
diagnostic classifies a **new observation**, not a10 or the earlier empty refresh.

No live request belongs to preparation. Independent implementation and protocol
review, merge and a separate coordinator assignment precede exactly one later
invocation under the standing necessary-test authorization. Preparatory merge
does not close #219; execution and independent result review remain separate.

## Exact finite invocation

Bind the reviewed merged commit, evaluator source blobs, existing Linux CPython
3.14 interpreter, clean execution directory and fresh evidence destination before
launch. Use the existing dedicated Client ID file. Agents inspect only file
metadata; only the supervised child reads its value. Substitute only the private
path in this template, never a credential value:

```sh
python -I -B evaluations/jamendo_stream/probe.py \
  --diagnose-metadata-changes /operator/private/client-id.txt
```

Use the existing supervised PID/wait/exit bookkeeping, without tracing or local
variable capture. Do not use `--send-to-jamendo`, the historical fixed-ID
`--diagnose-empty-refresh` mode, or internal functions for this assignment.
Default invocation remains disabled. No new environment or image is needed.

1. One GET to `https://api.jamendo.com/v3.0/tracks/`, fields in this order:
   runtime-only `client_id`, `format=json`, `type=single albumtrack`,
   `audioformat=mp32`, `limit=10`, `order=id_desc`, `ccnc=true`,
   `durationbetween=1_480`. Apply every existing response and candidate gate.
   Select only the first passing candidate in returned order. If none passes,
   stop; invalid rows are skipped locally without requests.
2. Immediately refresh that selected ID with one GET to the same endpoint:
   runtime-only `client_id`, `format=json`, `type=single albumtrack`,
   `audioformat=mp32`, `id=<selected ID>`, `limit=1`, in that order. No listing
   filters. Apply the existing envelope, identity, candidate and whole-row gates.
3. Stop unconditionally. **Equal evidence still makes zero audio requests.**
   A mismatch retains `refresh_evidence_changed`, exit 1 and
   `metadata_validated=false`. Equality reports `metadata_compared`, exit 0
   only with successful cleanup; `metadata_validated=true` means the evaluator's
   consistency check passed, never production admission. Other failures preserve
   their fixed reasons and exit 1. No retry or alternate candidate after refresh.

No redirect, address fallback, model, Discord, decoder, audio, download, browser
impersonation, format substitution or paid fallback is part of this mode.

## Fixed categories and normalization

The existing whole-row SHA-256 remains authoritative. It retains every returned
field; only integer/string duration and validated audio-query ordering are
normalized exactly as before. Opaque escaping/spelling, optional omissions,
nulls and unknown fields remain distinct; object key order is immaterial.

The same normalized row is partitioned into the eight fixed categories below.
Each partition gets one transient SHA-256; only eight fingerprints survive each
candidate parse. No partition contents or fingerprints enter reports or files.
Input remains bounded by the existing 256,000-byte metadata response cap; nested
values are hashed intact, never expanded into report keys. Empty partitions
are compared too, so addition, removal and null changes are visible.

| Fixed category | Exact top-level fields |
| --- | --- |
| `identity_credits` | `id`, `name`, `artist_id`, `artist_name`, `artist_idstr`, `creators`, `shareurl` |
| `album_type` | `album_id`, `album_name`, `type` |
| `duration` | `duration` |
| `rights_notices` | `license_ccurl`, `licenses`, `credits`, `notices`, `warnings`, `rights_warnings` |
| `details` | `description`, `details`, `musicinfo`, `releasedate`, `composition`, `composer` |
| `stream_route` | `audio` |
| `download` | `audiodownload`, `audiodownload_allowed` |
| `other` | Every other top-level field, including unknown names and nested values |

These are local structural buckets, not semantic findings. For example a changed
`other` does not disclose its key or mean the change is harmless; unchanged
`rights_notices` does not establish complete rights. A malformed/refused refresh
stops at its existing gate before comparison, with `evidence_changes=null`.
For two passing candidates, `evidence_changes` contains exactly the eight fixed
keys, each `changed` or `unchanged`. Multiple changes remain visible. Identity
mismatch still stops before category comparison. Stream mode adds these labels
but keeps its original refusal and audio-open gate.

## Bounds, redaction and settlement

| Boundary | Limit |
| --- | --- |
| Requests | At most two sequential metadata GETs/connections: one listing of at most ten candidates and one selected-ID refresh. No audio connection. |
| Bytes | At most 256,000 consumed decrypted HTTP bytes per response including status/headers/body, 512,000 aggregate; exact-cap responses refuse without sentinel reads. Not wire/TLS-buffer/kernel totals. |
| Buffers | Existing 16,384-byte header and 8,192-byte read bounds. Metadata bodies and comparison serialization are transient; no saved source body or audio. |
| Work | One unchanged 117-second supervisor deadline and child alarm cover startup, credential read, DNS/TLS, both requests and parsing. Never reset between calls. |
| Cleanup | Original three-second cutoff and retained sole-child owner. Timeout/interrupt kills and reaps; late reaping remains failed and can exceed the proposed 120-second total. No ownership abandonment or kernel-release claim. |
| Artifacts | One sanitized ASCII JSON report, all fresh invocation artifacts together at most 100 KB; total #219 evidence at most 5 MB within the standing 500 MB logs/exports ceiling. |

Existing safe transfer/count/first-refusal diagnostics remain. This mode withholds
`selected_track_id` (null); the selected ID is used only transiently for refresh.
Category reports contain no provider-controlled names, values, URLs, markers,
fingerprints or credentials. Stream counters remain zero and stream hash null.
The existing credential-substring suppression and report-size limit still apply.
No raw header, body, exception text, title or artist is retained. A missing child
report leaves accounting unknown within the finite caps; do not invent zeros.

Retain source/runtime/launch/exit receipts and the sanitized report within those
limits, observe reaping and source-bound process owners without reading process
environments, then stop for independent result review. Preserve earlier evidence.
No result permits another request or establishes a provider cause, source
compatibility, audio access, rights, completeness, quality, production capacity
or release readiness.

## Separate evaluator versus production contract observation

Static inspection at base `97c4af9fa9c5b708b51c95b62d6df560aa3ab8b3` confirms a
deliberate difference, not an explanation of a10. `probe.validate_candidate`
binds the complete row, including the opaque audio locator and incidental fields.
Production `jamendo_admission.parse_listing` excludes the locator and its
`_IGNORED` fields (including download fields, images and statistics) from policy
evidence. `jamendo_catalog.refresh` evaluates a fresh decision, rejects a changed
content digest and can return changed valid credits with `decision_matches=false`.
[ADR 0013](../../docs/decisions/0013-jamendo-discovery-streaming.md)
permits that fresh valid decision while requiring material content changes to
discard the ranked choice. This diagnostic neither adopts production semantics
nor weakens the evaluator. A separate decision/change would be needed to align
those contracts; the unknown a10 field supplies no justification for one.

## Offline verification

Run required locked sync, all pre-commit hooks and the full ordinary suite, plus:

```sh
uv run pytest -q evaluations/jamendo_stream
uv run pyright --pythonversion 3.14 --pythonplatform Linux --level error \
  evaluations/jamendo_stream
```

Synthetic regressions cover every category and mapped field, unknown/nested
changes, missing versus null, multiple changes, equivalent normalization,
unchanged whole-row digest encoding, malformed/refused inputs, fixed redacted
reports, the original alarm, and zero audio even for equal evidence. Existing
transport caps, redirects, cleanup schedules and stream refusal tests remain
applicable. These explicit evaluator tests are outside ordinary CI collection;
no test invokes a music source or production decoder.
