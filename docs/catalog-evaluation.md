# Catalog evaluation plan

Status: implemented offline selection contract and deferred optional comparison
for [issue #6](https://github.com/slackysoba/sobafm/issues/6) and
[Outcome 1](https://github.com/slackysoba/sobafm/issues/1). No real candidate has
been acquired, heard, rated or admitted by this work. No playback source is enabled.
The [requirements](requirements.md#first-source-evaluation) remain authoritative.

The optional comparison asks whether a bounded editorial or hand-picked selection
improves enjoyment over ordinary Jamendo results without losing request fit,
availability or useful variety. It does not qualify an entire catalog or establish
production reliability. The permission basis below is already confirmed; the
comparison measures selection quality, not whether that confirmation exists.

## Confirmed three-source permission

On 2026-10-27 the maintainer confirmed that the owners of **Jamendo, ccMixter
and Free Music Archive** explicitly permitted use of their music in SobaFM.
The [public authority record](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441099250)
identifies all three and carries forward the conditions recorded in the
[earlier clarification](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6440417895):
attribution is required; SobaFM must not be commercialized or monetized;
donations are permitted. This resolves the earlier source-identification and
direct-permission uncertainty, including FMA. No other source is included.

Use this maintainer-confirmed direct permission as the basis for this work.
Do not request the same grant again or replace it with a generic license-only
assumption. No private grant correspondence is required or published. Preserve
available recording licenses and notices as credit/provenance evidence; missing
metadata is a specific completion task, not evidence that the direct permission
is absent. Record any actual conflicting evidence concretely for review.

Service access, credentials, recording identity, complete credits and the
retrieval/storage route still need accurate records. The confirmation does not
itself authorize audio acquisition/listening in this preparation slice, change the
source-access policy, or enable a production source. Concise song status and
complete ordinary-listener private `/now` credits remain the product contract.
No Discord presence or permission changes are part of this preparation.

## Current selection contract

The [accepted decision](decisions/0002-prompt-catalog-selection.md) prioritizes
strong prompt matches and complete recordings across all three prepared sources.
Listening is optional deferred evidence, not a prerequisite for this implementation.

`sobafm.catalog` accepts `sobafm-catalog-1` JSON: a `synthetic` boolean and at most
one partition per source (`jamendo`, `ccmixter`, `fma`). Each partition declares
`complete`, `partial` or `unavailable`, a note identifying its scope/date, and
at most 256 records. These are bounded prepared subsets, not entire source
catalogs. An omitted partition is `missing`; a complete zero-record partition
is `empty`; a partial zero-record result stays `partial`. Report provided,
admitted and matched counts before the result limit, including zero matches.

Each recording preserves a stable source/recording key, title, primary artist,
all supplied creators, credential-free provenance, complete-recording evidence,
availability, optional duration/composition/composer, evidence-backed descriptor
facts and immutable credits. Each fact has one supported feature, boolean value,
strong/weak evidence and an evidence link. Unknown facts are omitted, never
inferred from a missing tag. False facts require evidence too. A source tag alone
does not automatically deserve strong confidence. Preserve upstream remix notices
in the credit snapshot; missing required information remains inadmissible.

The separate `sobafm-admission-1` review input is **trusted operator data**, never
provider/model output. Each review contains the source/recording key, canonical
`metadata_digest(recording)`, `credits_complete`, `access_ready`, matching
`synthetic` status, an evidence link and an optional bounded explanatory note.
True flags assert an actual scoped review;
the parser cannot establish that the assertion is true. SHA-256 binds the review
to the full parsed recording, including notices and descriptors; it is not a
signature or authorization service. Any metadata change needs a fresh review.
Admission does not prove successful audio retrieval, decoding or playback.

`parse_catalog`, `parse_reviews` and `parse_query` reject unknown fields,
duplicate identities/facts, conflicting constraints, invalid types, unsafe links
and unbounded data before selection. Catalog JSON is capped at 2,000,000 UTF-8
bytes; review JSON at 256,000 bytes; each recording at 32 facts, 16 creators and
16 notice paragraphs of at most 1,024 characters. Text forbids control/bidi
characters; links require HTTPS with no credentials, query or fragment. Preserve
complete credits or reject the import; never truncate required notices to fit.
Duration is an optional integer from 1 to 86,400 seconds, a finite metadata bound
that permits long recordings, not a claim of full-file duration or completeness.

The query model permits at most 16 soft and 16 required feature predicates,
inclusive minimum/maximum seconds, exact source/recording key, title, primary
artist, composition and composer, and up to 32 excluded recordings or creators.
Identity uses case-folded Unicode normalization and collapsed whitespace, not
fuzzy substitution. Creator exclusions check every supplied credited creator.
Hard constraints need known evidence; required facts must be strong. For soft
requests, a candidate must support at least one preference to remain a match.

All sources enter one pool. Sort by most strong supported preferences, fewest
contradictions, most weak supported preferences, fewest unknown preferences,
then longer known duration and stable source/recording key. Missing duration
sorts after known duration only when fit is tied. Return at most 20 results,
with separate strong/weak/contradicted/unknown facts, full credits, rejection
reasons and source coverage. There is no source preference or early-source exit.

`catalog_prompt` uses a small phrase table, not general language understanding.
It supports descriptor preferences, `no`/`without` plus a known descriptor,
`instrumental`, `only piano`, `play <title> by <artist>`, and numeric
`at least`/`at most`/`under`/`over`/`exactly` seconds/minutes/hours.
Unknown positive terms are returned explicitly. Unsupported exclusions,
mandatory wording, duration or identity syntax produce no executable query;
use validated explicit queries for constraints outside this grammar. The fixed
16 requests exercise bounded interpretation; they do not create listening scores.

Run the offline demo after the normal locked setup:

```sh
uv run python -m sobafm.catalog_demo "Quiet solo piano, no singing" \
  --catalog evaluations/catalog/selector-demo.json \
  --reviews evaluations/catalog/selector-demo-reviews.json --limit 3
```

The fixtures contain six invented recordings and five synthetic review records.
The complete ccMixter candidate ranks first, ahead of longer weaker matches;
the preview and unreviewed candidate are rejected. ccMixter coverage remains
partial. These files grant no real permissions and contain no music or media URL.
The same CLI accepts reviewed real metadata with `synthetic: false`; it does
not generate reviews or turn the earlier incomplete inventory into eligible songs.
It performs no provider calls, audio acquisition, listening or Discord activity.

## Deferred optional quality evidence

This slice supplies the [fixed requests](../evaluations/catalog/requests.json),
[run record](../evaluations/catalog/run-template.md),
[candidate record](../evaluations/catalog/candidate-template.md) and
[comparison record](../evaluations/catalog/comparison-template.md).
Copy the templates into a separately authorized run; leave their blank cells
blank until evidence exists. An unchecked box means unverified, not denied.

The subsequent [six-recording inventory](../evaluations/catalog/three-source-inventory.json)
and [finite acquisition/listening proposal](../evaluations/catalog/three-source-pilot.md)
record two proposed complete recordings per confirmed source, including access
gaps and poor metadata matches. This smaller preparation pilot is not J0/J1/C1
comparison evidence and does not replace the 16-request protocol below.

Only if a later listening comparison is requested, its execution requires:

1. Maintainer authorization for a concrete bounded run: source access, actual
   use context, candidate acquisition, storage and human listening. Record
   service/cost decisions before using the service; reuse the confirmed grants.
2. Recording-specific eligibility and complete credits before any audio request
   or playback, including evaluation playback. Metadata discovery alone is not
   admission. Resolve concrete missing credits/access evidence or conflicting
   rights before listening, without reopening the confirmed source grants.
3. Actual observations and human ratings, with missing cases retained.
4. A reviewed account of results and any proposed source/storage change. The
   accepted selector already compares all three sources; a comparison does not
   automatically enable playback or expand storage permission.

The selector's acceptance is deterministic checks and independent review, with
real-source admission gaps kept explicit. No human listening request is pending.
Ordinary CI must never initiate provider access, audio acquisition or listening.

## Optional fixed requests and comparison arms

`requests.json` contains 16 synthetic requests, all within the 200-character
command limit. R01–R14 span quiet and energetic music, vocals and instrumentals,
acoustic/electronic styles, deliberate lo-fi texture, and a language constraint.
R15 tests exact artist/title lookup; R16 tests a named composition without
assuming rights in any recording. No expected availability or rating is supplied.

Each request includes a human-authored interpretation and a proposed Jamendo
query mapping. Freeze these before retrieval. Use the same mapping for both
Jamendo arms and the same interpretation for the curated arm. No model call is
needed. Unrecognized tags or unsupported filters are observations; do not silently
relax a request. Change a mapping only in a new, versioned run applied to all arms.

| Arm | Candidate preparation | Frozen selection order |
| --- | --- | --- |
| J0: ordinary Jamendo | One result page per request; no editorial filter or operator taste screening | Provider relevance order after eligibility and duplicate-recording checks |
| J1: editorial Jamendo | Same query, plus `featured=1`, `boost=popularity_month`, `groupby=artist_id` | Returned order after the same eligibility and duplicate-recording checks |
| C1: hand-picked ccMixter/FMA | A frozen pool of at most 48 complete recordings, at most 24 from each source; record discovery route and curation rationale | Human descriptor match, then recorded curation priority, then stable candidate ID |

For J0/J1, propose `format=json`, `limit=10`, `offset=0`, `order=relevance`,
`type=single albumtrack`, `include=licenses musicinfo` and `audioformat=mp32`.
The request-specific parameters are in the fixture. Do not store a Client ID
or live audio URL in public records. These are a protocol, not executed calls.

The official [tracks documentation](https://developer.jamendo.com/v3.0/tracks)
describes featured selections, artist grouping and relevance-preserving boosts.
J0 leaves `boost` omitted; the documented default still applies a weaker monthly
popularity boost. It is not a popularity-free control. The documented default
`mp31` is 96 kbps and `mp32` is VBR; use the same format in both arms so encoding
does not confound the selection comparison. API behavior remains untested here.

Before C1 retrieval/listening, document a manual discovery route covering the
request styles, with a maximum of 48 inspected recording pages per source.
Choose no more than 24 candidates per source, retaining rejections and reasons.
Existing editorial recommendations or curator knowledge may inform admission;
record their origin and prior listening. This is intentionally a curation
treatment. Do not use the eventual listeners' scores to rebuild the pool.
Do not fill an unavailable source's quota from another source without a new run.

For C1 ranking, mark each request's `fit_checks` as supported, contradicted or
unknown from the frozen candidate descriptors. Sort first by fewest contradictions,
then most supported checks, then lower curator priority number, then candidate ID.
Unknown is not supported. Record descriptor provenance and confidence; metadata
match is not a human fit rating. Report C1 results by originating source as well
as in aggregate so one source cannot conceal the other's gaps.

## Bounded procedure

These proposed limits need acceptance in the future run record. They authorize
no activity by themselves and do not override a stricter provider limit.

1. Freeze the commit, request-set version, pool, query mappings, ranking,
   eligibility policy, reviewer aliases, date window and success criteria.
   Use two human listeners where practical; at least one must not have curated
   C1. A single-listener pilot is exploratory evidence only. This study-panel
   size is not a Discord audience limit.
2. Limit Jamendo discovery to 32 initial metadata calls: 16 requests in each
   arm, at most 10 returned candidates per cell. Permit one retry per failed
   call after the provider's required delay, with at most eight retries in
   total. A rate limit ends the batch until its retry window; never expand
   pagination to rescue a poor result. Stop at the approved time/call limit.
3. Review candidate rights before audio access. Keep all returned identities,
   original ranks, rejection reasons and retrieval failures in the comparison
   records. Deduplicate the same recording within each result list, preserving
   the earliest rank. Retain cross-arm duplicates and identify them.
4. Keep the first three eligible candidates in each cell, at most 144 candidate
   placements across 48 cells. Do not remove poor musical matches. Freeze all
   cells before comparison listening. An empty or blocked cell stays in the run.
   Do not select a replacement after hearing a disliked first choice.
5. Compare only rank 1 in this first pilot: at most 48 selections per listener.
   Rank 2/3 are metadata evidence of potential depth, not listening evidence.
   Hear each rank-1 recording from the beginning until natural end or 180 seconds,
   whichever comes first; use an explicit stop, not a rendered excerpt or fade.
   Record actual seconds, stops and incomplete auditions. This caps comparison
   audio at 144 minutes per listener, excluding breaks. Do not seek, overlap,
   loop, change pitch/tempo or make edited listening files.
6. Present requests in R01–R16 order. Rotate arm order J0/J1/C1, J1/C1/J0,
   C1/J0/J1 across successive requests; the second listener reverses each order.
   Do not display popularity, editorial badges or curation scores. Preserve all
   required credits and notices, even if that reveals the source. Record this
   as a credited comparison, not a double-blind study. Keep ratings independent
   until both listeners finish. Use the same device and comfortable non-clipping
   volume; document any gain change without exporting altered audio.
7. Allow at most one initial audio attempt and one retry per rank-1 selection
   per listener. Stop on rights conflicts, unexpected cost or exceeded limits.
   Count both attempts; retain first-attempt failures. An availability failure
   does not silently promote rank 2. Any recovery experiment belongs in a
   separately labeled follow-up.
8. Record the three ratings separately, summarize denominators and disagreements,
   then review the source decision. The same recording in multiple cells keeps
   all its placements; do not treat duplicated audio as independent evidence.

The run must also set finite network timeouts, response/transfer byte limits,
concurrency (proposed: one transfer), total transferred bytes and elapsed time.
Set audio disk limits, volume, retention and cleanup ownership before acquisition;
none are supplied as an implicit grant here. This documentation slice stores no
audio and needs no dependency installation. Runtime memory limits from the
requirements are separate from development disk or evaluation transfer budgets.

## Separate listening rubric

Use integers 1–5 only after a human audition. Blank means unobserved; zero is
not a score. An incomplete or unavailable audition has a reason and no rating
unless the listener explicitly records that the heard portion was sufficient.
State that limitation alongside any such rating.

| Score | Enjoyment: would I keep listening? | Fit: does this satisfy the stated request? | Fidelity: technical quality of the heard portion |
| --- | --- | --- | --- |
| 1 | Strong desire to stop | Unrelated or violates a core request | Severe technical damage prevents useful listening |
| 2 | Mostly unappealing | Weak resemblance; major constraints missed | Intrusive technical defects |
| 3 | Acceptable, little desire to return | Main idea present; meaningful compromises | Audible limitations, still usable |
| 4 | Enjoyable; would keep it on | Strong match; minor compromises | Clean enough; no distracting defect heard |
| 5 | Would actively choose again | Excellent match to all important constraints | No technical limitation noticed in the audition |

Enjoyment is personal; fit is tied to the fixed `fit_checks`. Record the check
missed rather than hiding it in a total score. For R15, only the requested
artist's exact recording can satisfy the identity requirement. R16 requires
the named composition but does not prescribe a performer. A substitute may be
enjoyable and still fail fit. No match must remain an honest outcome.

Fidelity notes separate clipping, corruption, dropouts, accidental silence,
truncation and encoding artifacts from composition, performance or taste.
Intentional lo-fi texture is not inherently a defect. An audition cannot prove
whole-file integrity or successful end-to-end decoding. Keep transfer status,
decoder completion and heard duration distinct in later technical evaluations.

Evaluate `mp31` versus `mp32` only as a separately authorized paired follow-up:
freeze up to six eligible Jamendo recording IDs, use both encodings of the same
recordings in alternating order, and record availability, transfer failures,
bytes, time-to-ready and fidelity. No automatic fallback changes the main run.
Nominal bitrate alone cannot establish quality or reliability.

## Coverage, variety, availability and effort

Use the [run template](../evaluations/catalog/run-template.md) for totals and
one [comparison record](../evaluations/catalog/comparison-template.md) for every
request/arm pair, including unattempted cells. Keep the selection sample distinct
from the much larger source catalog.

| Measure | Definition and denominator |
| --- | --- |
| Attempt coverage | Completed discovery cells / 16 requests per arm; blocked and unattempted counts alongside |
| Eligible coverage | Cells with at least one eligible candidate / 16; identity and rights failures remain visible |
| Audible availability | Rank-1 selections actually heard / attempted rank-1 selections, plus unattempted count and first/retry outcomes |
| Satisfying coverage | Cells whose rank 1 was heard and scored both enjoyment and fit at least 4 / 16, separately per listener |
| Listening scores | Per-listener median and distribution for each dimension; rated count, missing count and heard seconds alongside |
| Artist variety | Distinct canonical primary artists / nonempty rank-1 placements; list distinct artists across all three ranks separately |
| Repetition | Rank-1 placements minus distinct recording identities, divided by nonempty rank-1 placements; artist recurrence and adjacent repeats in R01–R16 order separately |
| Pool depth | Eligible candidates among the first 10 returned (J0/J1), or frozen pool (C1); report first-three fill rate / 48 possible placements per arm |
| Operator effort | Active minutes for discovery, rights, curation/descriptors, credits, acquisition and maintenance; elapsed waiting separately; minutes per admitted recording with count |

With a zero denominator report `not measurable`, not 0%. Keep ccMixter and FMA
subtotals. Canonical artist/recording identity must have evidence; unresolved
aliases yield a lower/upper variety range, not invented deduplication. The
repetition summary tests this request sample only, not a one-hour station or a
production no-repeat policy. Recheck rank-1 availability once 24–72 hours later
only if that metadata/audio access is explicitly in the run authorization;
otherwise durability remains unmeasured.

Compare J1−J0 and C1−J0 on the same requests and listener, reporting paired count,
median enjoyment/fit differences, wins/ties/losses and missing pairs. Never hide
unavailable selections by reporting only paired scores. Report all 16 requests,
and identify R15/R16 as lookup cases without discarding them after seeing results.

Proposed decision threshold, to accept or replace **before** listening: at least
12 rated pairs per listener, median enjoyment gain of at least one point, no
negative median fit change, and no reduction in satisfying coverage. Require
consistent direction across both listeners. Discuss fidelity defects, availability,
variety and upkeep explicitly; a numeric score cannot waive a rights failure.
If evidence is mixed or underpowered, retain the uncertainty and plan a bounded
follow-up rather than declaring material improvement. This is a pilot decision
rule, not a statistical significance or release-readiness claim.

## Eligibility, provenance and attribution

Complete a [candidate record](../evaluations/catalog/candidate-template.md) for
each distinct recording considered. A playable URL, download flag, subscription,
source tag or curator recommendation cannot establish permission.

Before admission, verify:

- Stable provider/recording identity and canonical credential-free landing page;
  title, version, supplied creators and any identified duplicate recordings.
- Complete recording rather than a stem, sample pack, loop or isolated vocal;
  evidence identifying incorporated sample/remix works and attribution parties.
- Permission basis and actual use conditions, using the confirmed direct grants
  above for these three sources. Preserve the supplied license version,
  jurisdiction and authoritative link where available; explicitly record missing
  notices and any concrete conflict. For other sources, establish their own
  permission basis. Do not infer NC status merely from self-hosting.
- Source access terms separately from copyright permission; authorized acquisition
  route, permitted storage, bounded retention and removal/recheck obligations.
- Immutable source-bound credit snapshot: supplied title/creators, notices,
  attribution parties, source backlink, license link and modification notices.
  Preserve the remix attribution chain, not only the final uploader.
- Safe complete credit rendering within the intended private `/now` bounds:
  validated links, no mentions or bidirectional controls, bounded pagination,
  ordinary-listener access and expiry. If required information cannot be safely
  displayed in full, do not admit it. This plan does not implement that UI.
- Named reviewer, date, evidence and scoped decision; distinguish approved for
  evaluation from approved for deployment. Missing, contradictory or unverifiable
  evidence remains pending or ineligible; never translate a blank into approval.

The [CC FAQ](https://creativecommons.org/faq/) explains that NonCommercial
depends on the actual use, that adaptations may need attribution to underlying
creators, and that platform terms and copyright licenses are separate. Resolve
license-specific ND/SA or legacy Sampling conditions using the actual text.
Catalog boundaries continue to follow the repository's successive-playback rule;
do not use this evaluation to authorize expressive edits or crossfades.

As one example, [CC BY 4.0 sections 2–3](https://creativecommons.org/licenses/by/4.0/legalcode.en)
cover reproduction/sharing and supplied attribution/notices, license links and
changes. It grants only rights the licensor controls and does not settle all
other rights or non-waivable royalties. Do not apply that example to a different
license version or assume a catalog-wide grant. Site removal does not itself
settle license validity; quarantine the candidate on a conflict and preserve the
dated evidence for review.

## Source-specific acquisition and storage gates

These are evidence-backed access constraints and proposed implementation gates,
alongside the confirmed permission basis. Re-read applicable terms at the authorized run and
at source integration. Public-document research date: **2026-10-27**.

| Source | Current authoritative evidence | Gate before acquisition or implementation |
| --- | --- | --- |
| Jamendo | [API terms](https://devportal.jamendo.com/api_terms_of_use), clauses 2, 3.1, 3.3 and 4.1: application registration, non-commercial API use, recording licenses, operational caching and creator/provider/backlink credits | Use the confirmed grant and retain complete notices. Resolve actual application access/Client ID separately. Use transient streaming buffers; no offline library. Bound necessary caching and refresh metadata as required. |
| ccMixter | [Query API statement](https://ccmixter.org/about) permits third-party application access. [Terms](https://ccmixter.org/terms), sections 4 and 8, describe attribution and recording conditions | Use the confirmed grant; select the complete mix and retain its incorporated-work credits. The pilot proposes bounded temporary copies, without assuming unlimited host bandwidth. |
| FMA | [App-developer policy](https://freemusicarchive.org/app-developers): API closed, no hotlinking, apps host permitted copies; forwarding user searches and scraping results needs explicit FMA approval | Use the confirmed grant through manually curated operator imports with complete notices and a hosting/storage decision. No runtime scraping or direct FMA playback. Repeated fetch/play/delete does not satisfy the hosting requirement. |

Jamendo's [download-method documentation](https://developer.jamendo.com/v3.0/tracks/file)
separates download permission from stream retrieval and documents rejection when
`audiodownload_allowed` is false. The flag does not resolve the use license.
Use the approved stream path for the planned streaming treatment; do not use a
download endpoint as a cache or availability workaround.

For ccMixter/FMA persistence, a later decision must name the operator-owned
volume, maximum bytes/tracks, accepted file types, validation before admission,
removal/eviction rule, recheck cadence and backup policy. Keep canonical metadata
and credit snapshots separate from disposable audio. Do not persist expiring or
credential-bearing audio URLs. No listener download or offline-player feature
is in scope. Deletion after use never expands permission.

The Jamendo and ccMixter pages and the CC references were read directly. Jamendo's
currently served terms show a 2013 last-modified date; this research does not
assert a new revision. The subsequent
[source-research checkpoint](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6440870726)
records readable FMA app-developer policy retrieved through Exa, resolving the
earlier policy-read gap. This slice also observed the policy in the public search
index; direct FMA page reads still returned 403. Its candidate records identify
the missing recording-level notices and formats individually. Do not bypass
access restrictions or mistake indexed metadata for current file availability.

## Evidence handoff

Publish implementation checks, review and remaining source-specific admission
gaps on #6. Preserve the distinction between a bounded metadata pool and live
source coverage. No subjective quality claim follows from deterministic tests.

If an optional listening study is later requested, publish populated observations,
failures and limitations with its own authorization and scope. Explain any
proposed source/storage change and record consequential choices in a new decision
record. Do not turn an unrun study into a selector acceptance or playback gate.
