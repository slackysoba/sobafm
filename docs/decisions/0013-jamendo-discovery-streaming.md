# 0013: Discover Jamendo recordings and stream under a checked first-play rule

- Status: Accepted design; implementation and qualification pending.
- Date: 2026-11-11
- Scope: [#179](https://github.com/slackysoba/sobafm/issues/179), recording the [detailed contract on #177](https://github.com/slackysoba/sobafm/issues/177#issuecomment-6793360608).
- Authority: [maintainer acceptance](https://github.com/slackysoba/sobafm/issues/177#issuecomment-6793493128): “Approve live search followed by ranking” and “Accept the proposed checked-streaming rule.”

## Problem and options

The implemented player ranks 167 admitted FMA recordings and reads local files.
That bounded library cannot supply broad source coverage. Jamendo discovery needs
both a validated search path and an explicit admission rule for a recording that
has not been reviewed or completely received before first audio.

The maintainer selected one interpretation call, bounded source search and one
pooled ranking call. A broad local metadata index would retain the initial single
ranking call but add ingestion, synchronization and stale-coverage costs. The
selected route starts with program-scoped candidate windows, not a metadata
database or general provider framework.

For first playback, the selected rule checks published information and available
notices before six-second readiness, then continues checking as the stream arrives.
The alternative was whole-stream validation and notice reconciliation before
playback, delaying first use by a complete transfer/decode and requiring a separately
justified temporary-storage or second-transfer design. A second transfer is not
automatically the same validated bytes; full validation cannot prove undisclosed
rights or subjective quality either.

## Supersession and unchanged scope

This record supersedes only these parts of the earlier accepted records, which
remain unchanged as historical records:

- [ADR 0002](0002-prompt-catalog-selection.md): prepared-only selection and separate per-record admission, including rejection of unknown completeness, **for Jamendo under the rule below only**.
- [ADR 0006](0006-gemini-play-matching.md): the prepared-catalog initial-call and data-disclosure contract.
- [ADR 0011](0011-program-refill.md): static-catalog exhaustion and the call bounds derived from a fixed shrinking catalog. Its timing estimates do not establish latency for a search plus ranking pipeline.

Best-fit disclosure, validated model output, eight choices per list, three refill
attempts with the existing backoff, failure preserving viable current audio and
local admission authority remain. [ADR 0007](0007-fma-dataset-batch-catalog.md),
the exact admitted FMA list and its separate manual reviews remain unchanged.
FMA still uses hosted copies, with no live scraping or hotlinking. ccMixter stays
disabled; other excluded sources stay excluded. Direct permission, attribution,
non-commercial use, unmodified catalog playback and source-specific storage
obligations do not expand.

## Search, ranking and bounded continuation

A new `/play` first produces validated musical intent through Gemini. Refusal,
invalid output or interpretation failure makes no source request and preserves
current music. Intent contains musical query fields, never arbitrary URLs or
tools; unvalidated raw request text cannot become a provider search. Search the
whole permitted Jamendo search space, including singles and album tracks, with
no featured-only restriction. Locally admitted results are ranked together with
all admitted untried FMA recordings. The model selects only distinct positions
from that supplied pool; it cannot approve candidates. Refills reuse the validated
intent, advancing the bounded search cursor without another interpretation.

Source-wide describes search reach, not exhaustive global ranking. Deduplicate
stable recording IDs and report partial, unavailable, disabled, missing and empty
partitions honestly. FMA may still supply a match when Jamendo is unavailable.
An empty bounded window does not mean the provider's catalog is exhausted.

| Bound | Contract |
| --- | --- |
| Jamendo search | At most two sequential pages of 100 per selection attempt; at most 1 MiB per metadata response |
| Candidate metadata | Existing 256-record/source envelope and 512,000-character matching ceiling; reject or degrade with explicit coverage, never truncate required credits |
| Song list | Ask for eight, best first; accept a shorter valid list as in ADR 0011 |
| Refill | Three attempts, with the existing 15-second then up-to-60-second backoff and duration-based second-wait cap; search is inside the attempt, with no nested retry multiplication |
| Concurrency | One selection pipeline per station and the shared four-model-call semaphore; existing per-model-call timeout and response bounds remain |
| Program lifetime | At most 768 selection cycles; retain at most 768 played/tried recording keys and end before history eviction, with an honest history-limit reason |
| No progress | Three no-progress attempts end discovery for that program; no hot loop or automatic history clearing |
| Persistence | Only necessary decisions and credit provenance in a documented bounded journal; no raw-response archive or mandatory new metadata database |

`/skip` uses the current program without reinterpreting the request or waiting for
a model answer. Cancellation and supersession discard stale results. Refill
takes no member cooldown. Stop, leave, listener-grace expiry, voice loss and
shutdown still cancel pending work while retaining its owners until settlement.

## Jamendo-only admission: `jamendo-stream-1`

The operator must enable this local policy; its default is disabled. Its authority
is the linked maintainer acceptance. Provider responses and model output cannot
enable or change it, set approval flags or supply trusted reviews. No agent
approves individual tracks.

**Accepted risk:** an ordinary identified Jamendo track listing with its normal
stream, known duration and no contrary evidence is a policy-accepted presumption
of a full recording, not proof. The source may be wrong or omit a credit; a stream
may prove incomplete later, after listeners have heard audio. The checked listing
and notices available before first audio are provisionally sufficient under this
exception. They do not establish that unexposed credits or rights do not exist.
The [tracks reference](https://developer.jamendo.com/v3.0/tracks) supplies no
verified-full-recording or complete-credit certificate. Single/album classification,
duration, a successful GET, a download flag, absence of negative keywords or a clean
decoder exit alone proves neither completeness nor rights. A verified completeness
claim needs source-bound full-version evidence and end-to-end validation; neither
exists in the live record.

Apply all of these conditions locally:

1. Require a matching stable Jamendo ID, nonempty title and creator, validated source backlink, positive finite duration, recognized Creative Commons license URI with its exact version and jurisdiction retained, and the ordinary streaming route. Reject missing or invalid required identity, rights or credits, contradictory notices, unresolved composition/sample-rights warnings and unreadable required credits. Never map an unknown license silently to a supported one.
2. Reject identified previews, excerpts, snippets, stems, loops and sample packs wherever type, title or supplied descriptions/notices identify them. Missing optional album information for a single is allowed. Do not invent a “complete” API field or import a 1–15-minute eligibility filter from #171.
3. Retain title, all supplied creators, Jamendo provider credit, track backlink, exact license and every supplied required notice. Reject rather than truncate. Inspect bounded leading embedded tags before readiness and later notices as they arrive. Additional required notice information or a conflict invalidates the decision and stops that attempt; never silently change the heard credit snapshot. A later attempt may evaluate a new coherent snapshot. Unknown or oversized notice structures that cannot safely be reconciled fail closed.
4. Record the recording key, canonical metadata digest, a policy-evidence digest of **every input used by this rule**, policy version, decision/reason and refresh time. Normalize admission-relevant evidence into that second binding, excluding secrets and stream URLs. Current `Recording.details` sits outside `metadata_digest`: a new preview tag must invalidate admission even if identity and credits did not change. A local decision proves policy application, not independent source truth.
5. A search-time decision permits ranking only. Immediately before **each** audio open, refresh by ID and re-evaluate, including cache hits. Invalidate the old decision first when metadata, evidence or policy changes. Changed valid metadata gets a new decision and credit snapshot, never inherited approval. A material identity or musical-content change discards the stale ranked choice and uses bounded continuation/refill. Failed refresh, removal/takedown or a known rights conflict permits no audio.
6. Bind prepared audio and rendered credits to that refreshed immutable decision. `/now` changes only when the exact source is heard. Refreshing one source cannot overwrite another audible recording's credits.

The [Jamendo API terms](https://devportal.jamendo.com/api_terms_of_use) remain the
source of provider obligations, including operationally necessary caching and
reflecting changes. Use transient streaming buffers, with no offline Jamendo
library or durable stream URLs. Deletion does not expand playback permission.

## First-play integrity and ownership

Readiness remains six seconds; a shorter recording requires completed verification.
Transport completion, decoder completion, valid PCM and audible drain remain
separate facts. Enforce finite encoded-byte, PCM and time bounds, HTTP framing,
MP3 validity, decoded-duration consistency and bounded streaming silence/clipping
checks. The transport implementation issue must fix its exact byte/time/notice
limits and conservative duration tolerance with offline cases before becoming
Ready. These engineering limits are not metadata proof or a new product decision.

On framing, corruption, truncation, duration, severe-defect or rights/notice failure
after readiness, stop emission, discard that stream's remaining buffered audio,
retain failure and cleanup ownership, mark the attempt failed/tried and use bounded
continuation. Preserve other playable current audio if a replacement fails before
switching. Do not automatically restart the failed track, pad, loop or claim the
whole recording played. Quarantine semantic/rights conflicts until corrected
evidence or operator review. Transient transport failures can be retried only
through a later bounded attempt, preserving the program's no-repeat history.

[ADR 0012](0012-three-station-capacity.md) remains exact: 268,435,456 group +
13,824,000 PCM + 12,582,912 copies = **294,842,368 controlled bytes**, charged
in full to every playing server; conditional aggregate PCM remains 101,552,640.
Three exclusive station pairs, all subdivisions, native limits, exclusions,
zero swap, 64 tasks and two CPUs remain. Reserve before external work; the fourth
station makes no calls. Metadata buffering must fit the whole-service group;
there is no new residual capacity. The original three-second cleanup clock,
failed verdicts and retained debt remain until actual release. The streaming path
must preserve the existing local-file size/hash checks and cannot add a decoder
slot or borrow another station's pair.

## Disclosure, cost and verification

Gemini receives the musical request and necessary musical context to produce
intent, then that intent/request and admitted descriptive metadata for ranking.
Jamendo receives validated musical query fields, without Discord identity. This
adds a model step and discloses musical search terms to another service; refills
repeat source search and ranking without a new listener command. No audio, Discord
identities, credit snapshots or credentials go into model prompts. `store=false`,
Google's unpaid data use and human review, age/regional restrictions, current model
and API choices, and the free-tier Lyria/no-spend decision remain as documented in
[provider access](../provider-access.md). Omitting identity fields does not make
arbitrary musical text anonymous.

Count calls and bytes, honor actual account/provider restrictions and configured
operator limits, and stop with an honest quota reason. #171's proposed 2 GB/day
and 30,000 calls/month are **not accepted allowances**. A deployment allowance
must fit intended station hours; actual mp32 traffic is unknown. This design
acceptance authorizes no live calls, spending, quota commitment or account changes.

Production still uses local FMA. Dynamic discovery and Jamendo streaming are not
implemented. The [one reviewed metadata success](https://github.com/slackysoba/sobafm/issues/2#issuecomment-6793174055)
establishes application access only, with no validated field values, recording
rights or completeness. No audio request occurred.

Implementation proceeds through admission/metadata, then independent search/ranking
and streaming producers, then station integration, as described in
[architecture](../architecture.md#accepted-jamendo-design-not-implemented).
The separately owned [#178 evaluator](https://github.com/slackysoba/sobafm/issues/178)
is not a code dependency or execution permission. Each implementation needs its
scoped offline checks and independent review. Separately authorized actual-image
and source qualification remains pending, including stream semantics, attribution,
three-station capacity/cleanup, 15-second median first audio and 60-minute continuity.
Historical results keep their original bindings. No new host is sought;
[#151](https://github.com/slackysoba/sobafm/issues/151) and
[#60](https://github.com/slackysoba/sobafm/issues/60) remain deferred.
