# Catalog evaluation plan

Status: documentation proposal for [issue #6](https://github.com/slackysoba/sobafm/issues/6)
and [Outcome 1](https://github.com/slackysoba/sobafm/issues/1). No candidate has
been acquired, heard, rated or accepted by this work. No source is enabled.
The [requirements](requirements.md#first-source-evaluation) remain authoritative.

The first comparison asks whether a bounded editorial or hand-picked selection
improves enjoyment over ordinary Jamendo results without losing request fit,
availability or useful variety. It does not qualify an entire catalog, establish
Discord playback rights, or prove production reliability.

## Deliverables and remaining gates

This slice supplies the [fixed requests](../evaluations/catalog/requests.json),
[run record](../evaluations/catalog/run-template.md),
[candidate record](../evaluations/catalog/candidate-template.md) and
[comparison record](../evaluations/catalog/comparison-template.md).
Copy the templates into a separately authorized run; leave their blank cells
blank until evidence exists. An unchecked box means unverified, not denied.

Later acceptance requires:

1. Maintainer authorization for a concrete bounded run: source access, actual
   use context, candidate acquisition, storage and human listening. Record
   service/cost decisions and required permissions before using the service.
2. Recording-specific eligibility and complete credits before any audio request
   or playback, including evaluation playback. Metadata discovery alone is not
   admission. Unknown or conflicting rights exclude a candidate from listening.
3. Actual observations and human ratings, with missing cases retained.
4. A reviewed source/storage decision supported by those results. The repository
   preference for editorial Jamendo and complete-track ccMixter is a hypothesis;
   FMA remains a possible complement. None is selected by this plan.

Merging this documentation cannot close #6 or satisfy Outcome 1. Ordinary CI
must never initiate provider access, audio acquisition or listening.

## Fixed requests and comparison arms

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
   C1. A single-listener pilot is exploratory evidence only.
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
  evidence for composition, recording and incorporated sample/remix rights.
- Exact license version/jurisdiction and its authoritative text; actual context
  of evaluation and intended music-only Discord use, commercial/NC constraints,
  additional permissions and any remaining conflict. Do not infer NC status
  from self-hosting or the absence of a ticket price.
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

These are evidence-backed constraints and proposed implementation gates, not
recording rights acceptance. Re-read applicable terms at the authorized run and
at source integration. Public-document research date: **2026-10-27**.

| Source | Current authoritative evidence | Gate before acquisition or implementation |
| --- | --- | --- |
| Jamendo | [API terms](https://devportal.jamendo.com/api_terms_of_use), clauses 2, 3.1, 3.3 and 4.1: application registration, non-commercial API use, recording licenses, operational caching and creator/provider/backlink credits | Confirm the application's service eligibility and credentials separately from each recording's rights. Use transient streaming buffers; no offline library. Bound necessary caching and refresh metadata as required. |
| ccMixter | [Query API statement](https://ccmixter.org/about) permits third-party application access. [Terms](https://ccmixter.org/terms), sections 4 and 8, require track-specific license compliance and attribution and do not warrant non-infringement | Select complete tracks; resolve incorporated material and notices. Choose temporary copies or a bounded operator library only where the specific recording and acquisition route permit them. Do not infer unlimited host bandwidth or generic streaming rights from the API statement. |
| FMA | [App-developer policy](https://freemusicarchive.org/app-developers): API closed, no hotlinking, apps host permitted copies; forwarding user searches and scraping results needs explicit FMA approval | Use manually curated operator imports with recording-specific copy/use permission and a hosting/storage decision. No runtime scraping or direct FMA playback. Repeated fetch/play/delete does not satisfy the hosting requirement. |

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
assert a new revision. FMA's policy was available through the search index
(reported crawled two weeks before research); direct retrieval returned 403.
Its [FAQ](https://freemusicarchive.org/FAQ), also indexed but not directly fetched,
says artists retain copyright and FMA cannot change their license terms. Obtain
readable current FMA policy and recording evidence before admission. Do not
bypass access restrictions or treat indexed text as completed qualification.

## Decision handoff

Publish the populated evidence, including failures and limitations, on #6.
Recommend one of: editorial Jamendo first; ccMixter complement; scoped FMA imports
for demonstrated gaps; or no selection yet. Explain improvements, missing styles,
rights boundaries, storage/maintenance cost and uncertainty. A combined C1 win
does not justify enabling both of its sources automatically.

The maintainer's accepted decision must identify enabled and excluded sources,
eligible license/use context, retrieval/storage policy, remaining implementation
and qualification work, and the evidence it relies on. Record consequential
choices in a new decision record; do not rewrite accepted decisions. Keep #6
open until its actual acceptance evidence and required decisions exist.
