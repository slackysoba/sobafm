# Bounded catalog search and refills

`catalog_search.py` implements the offline-tested search/ranking slice of
[ADR 0013](decisions/0013-jamendo-discovery-streaming.md) and the
[adopted implementation contract](https://github.com/slackysoba/sobafm/issues/188#issuecomment-6795394426).
It is wired into the station and Discord runtime by `CatalogRuntime` in
`discord_adapter.py`, including FMA-only configurations. This module owns no player, decoder, audio acquisition,
journal mutation, cooldown, retry timer or client shutdown. The a5 live evaluator
remains inconclusive and does not qualify this implementation.

## Consumer interface

Create one `CatalogSearch(catalog, reviews, *, client, metadata, policy,
observed_at)` per reserved station. `catalog` and `reviews` come from the existing
validated local catalog boundary. Only the FMA partition participates, through
the unchanged trusted-review admission function. ccMixter remains disabled.
`client` is the **same process-wide GeminiClient** used by other model work;
never create one client per station. `metadata` is that station's existing
`StationTransport` (`fetch(request, *, deadline) -> bytes | Refusal` and
`last_report`). `policy` defaults off. `observed_at` supplies aware UTC time.

| Call | Result and ownership |
| --- | --- |
| `await start(request, *, budget: ProgramBudget, current) -> Selection` | Exactly one validated intent call, then one search/rank attempt. Invalid input raises `CatalogInputError`; refusal or failed interpretation returns no intent or choices and makes no source request. No raw-request fallback. |
| `await refill(intent: Query, offset: int, *, budget, refill: RefillBudget, current) -> Selection` | Reuse the exact validated intent returned by start. No interpretation, retry loop or sleep. Cursor is page-aligned, from 0 through the terminal 153600 sentinel; the sentinel makes no source request but can still rank untried FMA. |
| `Selection` | Immutable `intent`, `next_offset`, `coverage`, up to eight `choices`, optional fixed `reason`, and safe `evidence`. Each `Choice` has the exact `recording`, `fit`, and Jamendo `candidate` (None for reviewed FMA). A ranking-purpose Candidate is **not permission to open audio**; the stream consumer must refresh it. |
| `current: Callable[[], bool]` | The caller's desired-generation predicate, checked before work and after awaits. Superseded results have no choices. Default True is useful only when the caller has no supersession. The caller must recheck its generation when consuming a result. |

Reserve station capacity **before** calling start. A fourth station must make no
model or source call. Keep the same CatalogSearch instance through same-station
replacement/cleanup: concurrent calls return capacity immediately and cannot
enter another pipeline. Cancellation propagates; the underlying transport joins
its original owners before the pipeline becomes available. Search does not close
the shared client or metadata scope. Stop/supersession must cancel the caller's
task and retain it until settlement, then apply the normal station release rules.

Search and pre-open refresh share the transport's metadata gate. Integration must
serialize their use of `last_report` when capturing diagnostics; this is a mutable
per-scope report, not a request-indexed log. Returned PageEvidence snapshots are
immutable. This slice does not write the provenance journal; the injected
transport continues to own its existing durable usage reservations and counters.

## Bounds and coverage

SearchWindow remains the sole request-sequence and admission authority. One
attempt requests at most two sequential pages of 100, at most 1 MiB per response.
Both receive the same absolute 20-second deadline; StationTransport also enforces
its own 10-second operation deadline, including admission wait. Cleanup retains
its independent original clock and may settle later. No transport or search
retry is added. Raw pages are released before the next fetch. Ranking waits for
the settled window: a conflicting or malformed second-page duplicate revokes
its earlier candidate.

The retained Jamendo candidate projection is limited to 256 records and
2,000,000 serialized UTF-8 bytes, including complete Recording/Decision/content
bindings and credits. The size is the canonical JSON array with sorted keys,
compact separators, Unicode retained, and the decision timestamp in ISO format.
One record is serialized at a time. An overflow rejects that entire source
window with explicit coverage; it does not shorten credits. The source parser's
bounded transient objects are still charged to the whole-service group.

Every eligible untried FMA recording is pooled with every remaining admitted
untried Jamendo candidate. The compact descriptive song list must fit 512000
characters. An oversized pool fails before model dispatch and sets
`prompt_overflow`; it never silently omits FMA to claim complete pooled ranking.
`rank_catalog(intent, recordings, *, client)` renders inside the shared four-call
gate, uses one existing 10-second call, store=false, 64000 response bytes and
2048 output tokens, and validates distinct positions, fits and at most eight
choices. Duplicate JSON fields are rejected too. Credits, decision evidence,
audio URLs, credentials and Discord identity are absent from model input.

Jamendo coverage is partial, empty-window, unavailable or disabled; it never
claims global exhaustion. FMA coverage describes a missing, partial,
unavailable, empty or complete **prepared subset**, not the whole source catalog.
Untried pool counts and separate window/prompt overflow flags explain what was
actually available to ranking. A failed Jamendo request may leave earlier valid
page candidates and/or FMA available. A wholly refused HTTP-200 window is
unavailable, with zero choices unless FMA independently supplies them.

`Evidence` contains at most two page snapshots (fixed transport category, numeric
HTTP status, dispatch/response/body counts, elapsed/cleanup facts and row count),
a histogram of existing Failure/Reason values, admitted/refusal counts, response
and canonical candidate bytes, elapsed time, stage and fixed model-failure/block
flags. `None` means an unknown row/body count, not zero. Rows count only validated
success envelopes; admitted counts are unique candidates after duplicate
revocation, while refusals count refusal events (including page-level failure).
They are not a per-row success equation. Evidence contains no source dump,
identity, request text, provider message or secret. Selection itself contains
musical intent and full source credits: log only its evidence/coverage/reason.
Transport body counters retain their documented application-body meaning and
unknown reservations; they are not wire billing or a new traffic allowance.

## Caller-owned continuation

Keep one ProgramBudget for the desired program. Every accepted pipeline start
increments `cycles` and `no_progress` **before** interpretation/search, including
failures and cancellation. A returned new admitted untried choice resets only
no_progress. At most 768 attempts may start; the 769th is refused. After three
consecutive attempts without progress no further attempt starts.

Before trying/preparing a choice, the caller checks its key against `budget.tried`
and calls `mark_tried(key)`. It retains all played/tried keys, including failed
preparations, and ends before a 769th distinct key could be inserted. No eviction
or history reset occurs. Recheck history when consuming delayed choices; search
also filters history again after ranking and refuses a result if history filled
while it waited. The last allowed successful cycle may still supply its choices;
the cycle limit denies the next selection, not audio already admitted to the list.

Create one RefillBudget for each list-depletion episode and preserve it across
cancellation/failed replacement. Pass it to every attempt of that episode.
Three attempts are allowed, each containing the search and at most one ranking
call. Success resets no-progress only; it cannot refund refill or lifetime debt.
After consuming a genuinely new list, its next depletion gets a new RefillBudget.
Never create a new budget merely to retry a failure.

`refill.delay(seconds_left)` supplies the existing delay before another attempt:
15 seconds after the first, then max(15, min(60, seconds_left - 16)) after the
second. Unknown/nonfinite remaining duration uses 15 seconds. After three it
returns None. The caller owns the interruptible wait and must also honor the
transport's quota/not-before restrictions. These waits do not promise the old
fixed-catalog latency: source search adds up to 20 seconds. Do not add another
retry loop. `/skip` consumes existing choices immediately and never calls start,
reinterprets, waits for model work or resets a budget.

On an empty/failing third attempt Selection reports the lifetime ending or
refill_failed. Earlier failures expose their fixed cause; the caller may retry
only within the remaining budget and backoff. Empty windows are not catalog
exhaustion. Existing playable audio and immutable heard credits remain entirely
under the station's ownership through all these outcomes.

## Verification and remaining work

Deterministic synthetic tests exercise intent refusal/invalid input, pooled
singles/album/FMA matching, duplicate revocation, partial/all-refused responses,
credit/prompt/window bounds, privacy, stale completions, joined cancellation,
shared model concurrency and exact continuation bounds. No provider, music
source, Discord, media, native decoder or image request is part of these tests.

Station integration consumes these merged interfaces and the checked stream,
with its ownership and budgets described in [architecture](architecture.md). Actual-image useful fit, source
compatibility, music quality, attribution delivery, first-audio latency and
continuous playback remain separately authorized qualification work. No numeric
resource partition, original cleanup deadline, provider allowance or release
claim changes here.
