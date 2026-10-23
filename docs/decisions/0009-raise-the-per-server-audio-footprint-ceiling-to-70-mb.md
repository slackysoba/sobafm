# ADR-0009: Raise the per-server audio footprint ceiling to 70 MB

- **Status:** Proposed until this decision pull request is accepted and merged
- **Date:** 2026-10-22
- **Issue:** [#218](https://github.com/slackysoba/sobafm/issues/218), parent [#175](https://github.com/slackysoba/sobafm/issues/175), milestone [#114](https://github.com/slackysoba/sobafm/issues/114)
- **Supersedes on acceptance:** only the total audio-footprint ceiling and fixed parent split identified below

## Context

The maintainer [explicitly authorized increasing the per-playing-server audio
allowance from 40 MB up to 70 MB](https://github.com/slackysoba/sobafm/issues/175#issuecomment-6343335401)
to simplify conservative reservations and advance qualification. Within that
authorization, the orchestrator selected a strict total ceiling of 70,000,000
bytes. This record makes that choice reviewable and reconciles NFR-3/NFR-6;
the concrete record remains Proposed until its decision pull request is accepted.

The existing [shared accounting contract](../architecture.md#shared-audio-accounting)
requires complete reachable reservations, including copies, overshoot and cleanup
debt. A larger ceiling permits straightforward whole-buffer and component
reservations where further optimization solely to fit 40 MB would add complexity.
It supplies no missing allocation bound, release signal or qualification result.
The fixed 38,000,000-byte parent split must also be replaceable so that it does
not prevent use of the authorized total increase.

## Requirements

1. Keep all attributable resident audio for one playing server strictly below
   70,000,000 decimal bytes. Include every retained source and allocation across
   both modes, not only presently producing or audible sources.
2. Preserve aggregate PCM at most 34,560,000 bytes. Require an explicit finite,
   qualified parent/native reservation split within the total before admission.
3. Preserve normal playback/session/spare/fade behavior, ADR-0008's exceptional
   bounded-input failure policy, pre-input native containment, the original single
   three-second cleanup deadline and debt until actual release.
4. Preserve supported-platform, rights, source/notices, operational and shipping
   gates. Confine runtime changes to the existing ledger's 70,000,000-byte total
   and derived integer parent bound. Keep the decoder's 128 MiB default,
   dependencies, production trust and qualified envelopes unchanged.

## Options considered

| Option | Benefit | Cost and limit |
| --- | --- | --- |
| Keep 40 MB and optimize each component further | Retains the smaller per-server allowance. | More implementation complexity and qualification work solely to fit the earlier ceiling; complete ownership proof is still required. |
| **Use the authorized strict 70 MB total with a qualified parent/native split** | Supports simpler conservative reservations using existing owners and the shared ledger. | Permits more attributable audio per playing server; each actual profile still needs complete bounds and qualification. |
| Select an intermediate total below 70 MB | Allows some increase with a smaller maximum footprint. | Constrains conservative reservations without a demonstrated profile that needs that particular intermediate ceiling; supplies no additional ownership or release proof. |

## Decision

Select the strict **70,000,000-byte total** and replace the old fixed parent split
with a qualified parent/native reservation split within that total. There is no
independent fixed parent partition. Here 70 MB means 70,000,000 decimal bytes;
equality is ineligible. The invariants are:

- Aggregate PCM remains at most 34,560,000 bytes.
- The permitted parent reservation follows the native obligations still charged
  through the strict combined-total check.
- Each profile identifies finite maximum parent and audited native audio
  reservations, including their simultaneous reachable growth. Parent audio
  includes PCM and other parent storage. Their combined total is strictly below
  70,000,000 bytes; all attributable storage must belong to that complete split.
- One station byte ledger covers both engines and retained cleanup debt across
  replacement stations. Reserve before allocation/retention; count aliases of
  one backing allocation once and separate copies separately.

The total includes retained live/incoming/spare/ended/filling Lyria and catalog
sources, encoded/raw input, JSON/base64/decoded objects, native and child audio,
HTTP/pipe/TLS/socket queues, player/encoder/transport buffers, assembler tails,
copies, overshoot and cleanup debt through rotation, recovery and mode changes.
Process RSS, total process/Job commit, producer counts and nominal PCM arithmetic
do not establish that attributable-audio bound. The child containment boundary
remains separate from the audited native audio reservation.

The existing `AudioLedger` changes its total constant and derives its retained
`PARENT_LIMIT` as `TOTAL_LIMIT - 1`, the largest integer below the strict total.
Its PCM/parent comparisons, strict combined-total comparison and lease/unknown-debt
behavior remain intact. The derived parent bound imposes no separate partition:
every still-charged native reservation reduces the parent allowance.

Hypothetical arithmetic admits a 50,000,000-byte parent reservation with a
provisional 4 MiB native/outside reservation: 54,194,304 bytes combined. One
additional 8 MiB child fits; a second would exceed the total and is refused.
This demonstrates admission arithmetic only; the outside classification and
complete native, transport and player envelope remain unqualified. Production
trust and the decoder's 128 MiB default are unchanged. A qualified production profile still
requires scoped implementation/review and applicable
[#175 qualification](https://github.com/slackysoba/sobafm/issues/175). Unknown or
infeasible obligations continue to block catalog admission. No ready spare is
discarded, admitted PCM trimmed or normal replacement generation suppressed to
make reservations fit.

### Exact supersession scope

Accepted records remain byte-for-byte intact. On acceptance, only these numeric
footprint provisions are superseded; every other provision remains in force:

| Accepted provision | Scoped replacement |
| --- | --- |
| [ADR-0004 requirements item 4](0004-stream-lyria-realtime-through-buffered-decks.md#requirements) | Replace its 40 MB audio-footprint ceiling with strict 70,000,000 bytes. Preserve bounded Lyria usage and all playback constants. |
| [ADR-0006 requirements item 5](0006-play-catalog-and-generated-music-in-one-station.md#requirements), [combined budget gate](0006-play-catalog-and-generated-music-in-one-station.md#combined-budget-and-qualification-gate), [criteria reconciliation](0006-play-catalog-and-generated-music-in-one-station.md#explicit-reconciliation-of-original-crossfade-criteria) and [qualification table](0006-play-catalog-and-generated-music-in-one-station.md#acceptance-and-subsequent-qualification) | Replace only the 40 MB/40,000,000-byte total and 38,000,000-byte fixed parent sublimit with the qualified split against the strict 70,000,000-byte total. Keep PCM, accounting, retained-source, cleanup and qualification rules. |
| [ADR-0007 guardrails and accounting](0007-bound-catalog-audio-decoding.md#engineering-guardrails-and-readiness-gates) | Replace only the 38,000,000-byte parent ceiling and 40,000,000-byte combined audio ceiling with the qualified split and strict 70,000,000-byte total. Keep decoder/input/queue/time/containment/source obligations. |
| [ADR-0008 requirements item 3](0008-fail-lyria-producers-at-qualified-ingress-bounds.md#requirements) | Replace only its fixed parent and total audio ceilings. Keep PCM, complete ingress accounting, explicit failure policy, supported interfaces, diagnostics and actual-release rules. |

Historical nominal-buffer arithmetic and earlier measurements remain historical;
they are not recalculated into headroom or a qualification pass. Whole-track
decoding remains excluded by ADR-0007's incremental streaming contract.

## Consequences

The allowed attributable-audio footprint increases by up to 30,000,000 bytes per
playing server. Operators must size deployments for the qualified profile they
actually use. A larger total can simplify conservative allocation/copy bounds,
but does not increase the PCM ceiling or establish useful catalog capacity.

The original single monotonic three-second cleanup budget, cancellation-safe
owner and actual-release conditions remain unchanged. Unfinished tasks,
transports, process trees or retained audio keep their leases/debt/slots across
station replacement; a timeout, producer exit or source removal refunds nothing.

[#175](https://github.com/slackysoba/sobafm/issues/175) still owns complete
allocation/release, useful-capacity and supported-platform qualification. Existing
failed/incomplete observations retain their original scope. [#130](https://github.com/slackysoba/sobafm/issues/130)
clean-machine BOTH-mode guidance, [#131](https://github.com/slackysoba/sobafm/issues/131)
live/endurance/latency and [#132](https://github.com/slackysoba/sobafm/issues/132)
shipping remain required, alongside legal/source/notices and native supply gates.
The ledger ceiling revision supplies no transport, dependency, readiness or shipping pass.

## Revisit triggers

- A complete qualified profile cannot fit the strict total while preserving PCM,
  normal generated behavior and all actual-release obligations.
- Deployment capacity or operational evidence requires a different total ceiling.
- Proposed work changes PCM, playback, containment, cleanup, supported-platform,
  rights/source obligations or another provision outside this narrow supersession.

Such changes need their own scoped review; this record grants only the total
footprint and parent-split revision above.
