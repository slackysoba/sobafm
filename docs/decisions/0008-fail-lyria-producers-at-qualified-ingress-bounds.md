# ADR-0008: Fail Lyria producers at qualified ingress bounds

- **Status:** Accepted on merge of PR #206; Proposed until then
- **Date:** 2026-10-21
- **Issue:** [#203](https://github.com/slackysoba/sobafm/issues/203), parent [#175](https://github.com/slackysoba/sobafm/issues/175), milestone [#114](https://github.com/slackysoba/sobafm/issues/114)
- **Refines on acceptance:** the exceptional Lyria failure clauses identified below; other provisions of ADR-0004/0006/0007 remain in force

## Context

The [maintainer's recorded choice](https://github.com/slackysoba/sobafm/issues/203#issuecomment-6315404910)
is **“Allow bounded producer failure (Recommended)”**. This record documents the
selected exceptional-failure policy and its limits. It does not implement it or
select an ingress allowance. The choice is resolved; acceptance of this concrete
record occurs through maintainer review and merge of PR #206.

[NFR-3/NFR-6](../requirements.md#non-functional-requirements) require the complete
attributable resident audio envelope to fit before retaining allocations. The
60-second Lyria pause threshold is flow control, not a hard allocation limit or
an enforceable bound on audio still arriving after pause. Already-buffered audio
and ready spares must remain available under the existing lifecycle rules.

The source/API-contract finding recorded on #203 concerns the locked
google-genai 2.26.0 and WebSockets 16.1.1 integration:

- The SDK's [music connection](https://github.com/googleapis/python-genai/blob/v2.26.0/google/genai/live_music.py)
  accepts the model but exposes no transport limit, connection factory or
  independent close-observer configuration. Its receive path decodes a whole
  message before returning audio to the application.
- The [versioned WebSockets API](https://websockets.readthedocs.io/en/16.1.1/reference/asyncio/client.html)
  offers flow-control and closure APIs, but the SDK does not expose those seams.
  Pausing socket reads behind queued audio cannot guarantee observation of a
  close frame later in that same stream.
- The [official music protocol](https://ai.google.dev/api/live_music)
  documents pause and faster-than-playback generation, but establishes neither
  a finite post-pause byte allowance nor an acknowledgement carrying that bound.

This is an unresolved supported-interface and allocation-proof gap, not a
measured overrun, provider misbehavior or proof about every possible future SDK.
Losslessly accepting an unbounded tail cannot be the fallback for finite memory.

## Requirements

1. Preserve every already-admitted PCM frame under ordinary source ownership,
   playout and spare-retention rules. Do not silently trim audio or discard a
   playable fallback to make an allocation fit. This does not promise that every
   frame is heard after an explicit stop, replacement or voice loss.
2. Keep normal generated behavior unchanged: 540-second retirement, 60/45-second
   pause/resume, six-second pre-roll, two-producer admission, session reservations,
   spare choice, failure/refusal ordering, retries, fades and crossfades.
3. Keep aggregate PCM at most 34,560,000 bytes, parent audio at most 38,000,000
   bytes and all attributable resident audio strictly below 40,000,000 bytes.
   Derive any allowance from complete reachable owners, including retained
   sources, raw input, TLS/socket queues, JSON/base64/decoded objects, copies,
   overshoot, player/native storage and cleanup debt. No numerical Lyria
   allowance or nonzero catalog headroom is established here.
4. Preserve the single original three-second total cleanup budget and its
   cancellation-safe owner. Failure, retirement, producer exit, empty queues,
   source removal and timeout do not prove storage release or refund debt.
5. Use supported, reviewed transport/configuration interfaces and prove complete
   allocation and release before enabling the affected readiness path. A guard
   after an unbounded SDK allocation is not that proof. Keep unknown obligations
   fail-closed and production catalog trust empty until its prerequisites pass.
6. Use explicit safe failure feedback and diagnostics under OPS-5/FB-3. Do not
   misreport this outcome as normal retirement, completion or remote closure,
   and do not expose raw provider input, credentials or exception text.

## Options considered

| Option | Fit and reuse | Costs and limits |
| --- | --- | --- |
| **1. Exceptional bounded-input producer failure (selected)** | Reuse the supported SDK/transport when a reviewed finite seam is available, existing source failure/recovery, buffered playout and shared accounting. End production before retaining input beyond its complete reservation. | Changes exceptional failure behavior and can exhaust available buffered audio. It still needs a supported interface and complete allocation/release proof; choosing the policy alone supplies neither. |
| **2. Keep the existing guarantee and remain fail-closed** | Make no exceptional playback change. Wait for a supported ingress/close seam and an enforceable finite service-tail contract. | The available integration has no established joint proof of finite ingress, continued lossless receipt and prompt close observation behind a full paused backlog. Readiness stays blocked on those missing prerequisites. |

The maintainer selected option 1. Private SDK access, monkeypatching, a custom
transport/protocol, a different provider, extra cost and larger limits are not
approved alternatives in this decision. Prefer a supported upstream SDK seam;
any actual transport or dependency change needs its own scoped review.

## Decision

When a producer continues delivering audio after pause beyond what its complete,
derived and pre-reserved ingress allowance can retain, SobaFM may end that
producer as an **explicit bounded-input failure before admitting the excess**.
It must preserve all already-admitted PCM for the normal playout/retention rules
and invoke the existing failure, spare, retry and fade behavior. It must not
silently discard excess audio while continuing to call the producer healthy.

Ending production does not immediately release its session slot, audio lease or
cleanup ownership. Each follows its existing actual-end or actual-release
condition; outstanding storage and cleanup debt survive station replacement.
Do not suppress ordinary replacement generation or discard a ready spare to
create fictitious headroom. Normal stop/replacement/voice lifecycle semantics
remain unchanged.

The exception permits a local terminal failure instead of requiring indefinite
receipt or waiting for a remote close hidden behind a full backlog. For input
that remains admissible, preserve existing paused-session close observation and
retirement/cancellation unblocking. A successful pause call does not establish a
remote acknowledgement or a finite tail. Reservation exhaustion must not be
treated as a normal service condition that satisfies the unchanged latency or
endurance criteria; failures and underruns retain their actual outcomes.

### Exact relationship to accepted decisions

Accepted ADR files remain unchanged. On acceptance, this record refines only the
following provisions for the exceptional ingress-exhaustion path:

| Existing provision | Scoped refinement; everything else remains unchanged |
| --- | --- |
| [ADR-0004 Requirements item 3](0004-stream-lyria-realtime-through-buffered-decks.md#requirements), and its [Decision buffers/handover rules](0004-stream-lyria-realtime-through-buffered-decks.md#decision) | Supersede any unconditional reading that receipt or gap-free recovery must continue after a complete ingress reservation is exhausted. Production may fail explicitly; admitted PCM, normal buffer playout, fallback selection and generated transitions remain preserved. Normal-service PLAY-3/NFR-1/NFR-2 targets are unchanged. |
| [ADR-0006 Requirements item 2](0006-play-catalog-and-generated-music-in-one-station.md#requirements) and [reconcile/regression rules](0006-play-catalog-and-generated-music-in-one-station.md#reconcile-order-and-lyria-regressions) | Refine blanket generated-behavior preservation only to admit this additional exceptional producer-failure condition. Do not change normal retirement, failure accounting/order, retry/refusal, session, spare, fade or cross-mode ownership rules. |
| [ADR-0006 combined budget gate](0006-play-catalog-and-generated-music-in-one-station.md#combined-budget-and-qualification-gate) and [ADR-0007 guardrails/accounting](0007-bound-catalog-audio-decoding.md#engineering-guardrails-and-readiness-gates) | Supply the reviewed policy target contemplated when preserved behavior has no proven finite fit. Do not replace their ceilings, accounting, cleanup, supported-installation or fail-closed qualification obligations; this decision does not establish a feasible envelope. |

No catalog rights, successive boundary, completion, private-credit, privacy,
security, distribution or cost decision is superseded. Neither ADR-0004's
historical observed chunk sizes nor nominal buffer arithmetic becomes a bound.

## Consequences

The selected policy supplies a finite correctness target without an undocumented
provider promise. A future implementation can report exhausted ingress rather
than accepting unreserved allocations or concealing data loss. The tradeoff is
an exceptional producer failure, potentially followed by underrun when existing
fallback audio is insufficient; the record grants no new success claim.

Follow-on work must first establish a reviewed feasible design using supported
configuration/admission interfaces and covering every reservation/release
obligation, then implement and review the narrowly scoped failure path. Complete
actual allocation/release proof and applicable qualification must precede
readiness, not the source work that makes those checks possible. Test admissible
paused close handling, delayed/failed pause and bounded-input exhaustion; retain exact
PCM and a ready spare through failure; cover retirement, voice pauses, mode
changes, repeated cancellation and debt across station replacement. A cap that
only bounds an application deque, or a counter after whole-message decoding,
does not establish the required resident bound.

[#175](https://github.com/slackysoba/sobafm/issues/175) retains the allocation,
release, useful-capacity and supported-platform qualification. This record runs
no provider/native experiment and grants no production implementation, ingress
value, transport, readiness or shipping approval. [#131](https://github.com/slackysoba/sobafm/issues/131)
live/endurance/latency, [#130](https://github.com/slackysoba/sobafm/issues/130)
clean-machine BOTH-mode guide verification and [#132](https://github.com/slackysoba/sobafm/issues/132)
shipping remain mandatory where unperformed. Existing failed/incomplete evidence
and consumed-operation limits remain intact.

## Revisit triggers

- A supported SDK/service contract supplies finite ingress, pause-tail and
  closure guarantees, or changes the owners/overshoot that must be reserved.
- A complete qualified envelope cannot fit the existing limits while preserving
  admitted PCM, ordinary generation/spares and actual release obligations.
- Proposed work would change normal generated behavior, discard admitted audio,
  alter any cap, use a private/custom transport, or add cost/security exceptions.

Any such material revision returns to a scoped maintainer decision; this record
is not standing authorization for those changes.
