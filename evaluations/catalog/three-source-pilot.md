# Three-source metadata inventory and listening proposal

Status: **metadata preparation complete; optional listening proposal deferred**.
Issue: [#6](https://github.com/slackysoba/sobafm/issues/6). Observation date:
2026-10-27. Inventory: [three-source-inventory-1](three-source-inventory.json).
Requests: [catalog-requests-1](requests.json). No audio, ratings, source selection
or production enablement is claimed. This historical proposal is retained for
optional future use; no listening approval is currently requested.

## Existing authority and purpose

The maintainer has confirmed direct permission from the owners of Jamendo,
ccMixter and Free Music Archive to use their music in SobaFM, with attribution,
no commercialization or monetization, and donations permitted. The
[authority record](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441099250)
and [conditions](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6440417895)
are the permission basis for all six records. No private correspondence is
requested. No other source is added.

This preparatory inventory checks whether a small set can be identified,
credited and acquired through the intended routes. Its hand-picked songs are
not J0/J1 search results and cannot establish improvement over ordinary Jamendo.
The [accepted selection decision](../../docs/decisions/0002-prompt-catalog-selection.md)
defers listening and compares all three prepared sources by supported prompt fit.
The [16-request comparison](../../docs/catalog-evaluation.md) and this pilot are
optional human-quality evidence, not implementation acceptance gates. These six
records can still identify credit/access completion work; they are not admitted
selector inputs and do not establish successful retrieval or music quality.

## Six proposed complete recordings

The inventory contains the field-level evidence, source IDs, dates, creator
names, reported formats/durations, license labels, draft credit snapshots and
explicit gaps. A source's published duration is not a measured file duration.
Complete-recording evidence is metadata evidence only; actual content is untested.
`null` means unknown, never that no contributor or notice exists.

| Candidate | Recording / creator | Request | Reported duration | Evidence and present limitation |
| --- | --- | --- | --- | --- |
| JAM-1848357 | [mañana será tarde — fankel](https://www.jamendo.com/track/1848357) | R06 funk | 4:32 | Official API documentation example only; rock/bass/groovy tags leave funk and prominent bass unconfirmed. Live record and complete notices unavailable. |
| JAM-1880336 | [Queensland [ High Energy Funk Instrumental ] — Lofi Lopez](https://www.jamendo.com/track/1880336) | R06 funk | 2:27 | Same documentation example; funk/high-speed descriptors, bass prominence unknown. Live record and complete notices unavailable. |
| CCM-17241 | [Sunrise (unplugged) — Admiral Bob, featuring Shannon Hurley](https://ccmixter.org/files/admiralbob77/17241) | R04 acoustic folk | 3:12 | Readable editorial remix; acoustic/vocal evidence. Linked upstream Sunrise upload unavailable, so its full notices remain unknown. |
| CCM-70993 | [WILD WINDSTEPS — Ant.Survila, featuring Dan_Mantau](https://ccmixter.org/files/AntSurvila/70993) | R03 ambient | 2:09 | Readable editorial instrumental; mood/percussion unconfirmed. Retain the Fruitless chain through Lisa DeBenedictis without asserting her voice is in this mix. |
| FMA-NIGHT-OWL | [Night Owl — Broke For Free](https://freemusicarchive.org/music/Broke_For_Free/Directionless_EP/Broke_For_Free_-_Directionless_EP_-_01_Night_Owl/) | R08 electronic | 3:14 | Indexed official record; direct page returns 403. Source says Instrumental No, a mismatch to retain. Exact license link, full notices and format unknown. |
| FMA-GYMNOPEDIE-1 | [Erik Satie: Gymnopedie No 1 — Kevin MacLeod](https://freemusicarchive.org/music/Kevin_MacLeod/Classical_Sampler/Gymnopedie_No_1) | R16 composition | 3:07 | Indexed official title and instrumental flag; solo piano unverified. Direct page returns 403; exact license link, full notices and format unknown. |

The two Jamendo identities came from the currently served official
[tracks-method example](https://developer.jamendo.com/v3.0/tracks), not an executed
API query. It supplies CC BY-NC-ND 3.0 links, artist IDs and album positions.
Neither live availability nor editorial selection can be inferred from it.
Use these as provisional candidates; retain them if a current lookup fails.

ccMixter discovery used its [editorial/all-time list](https://ccmixter.org/view/media/picks)
and the linked recording pages. The two nominated mixes report CC BY-NC 3.0.
FMA discovery used targeted title/composition searches exposing official track
metadata. FMA's [artist guide](https://freemusicarchive.org/artist_guide) describes
an old default of Instrumental No; this is a reason to verify Night Owl's field,
not permission to replace it with a guessed value. No personal prior listening
informed this inventory. Editorial placement and popularity are discovery signals.

The inventory also preserves two inspected alternatives: **sandgrains** by
airtone (male vocals conflict with R03), with its published Speck/Jeris/airtone
chain, and Kevin MacLeod's **Erik Satie: Gymnopedie No 2** (wrong work for R16,
direct page unavailable). Supporting stems/a cappellas are credit evidence only;
they are not additional audition selections. No failed candidate is silently
replaced after listening. Incidental search hits were not ranked as catalog results.

## Complete-credit handoff

Each dated draft combines its candidate's title, displayed creators, provider
credit, canonical backlink, available license label/link and modification notice
with the linked upstream-credit records. Preserve the original revision when
new evidence arrives. Unknown notices stay visibly incomplete. Before admission,
freeze a new complete source-bound snapshot with reviewer/date and evidence;
verify the actual file's identity and supplied notices without changing the grant.

For Sunrise, preserve both Admiral Bob and Shannon Hurley, the parent remix and
the unavailable [Sunrise source](https://ccmixter.org/files/shannonsongs/15139).
For WILD WINDSTEPS, preserve Ant.Survila and Dan_Mantau plus the linked
[Dan_Mantau Fruitless](https://ccmixter.org/files/Dan_Mantau/70072) and
[DeBenedictis Fruitless](https://ccmixter.org/files/lisadb/25112) notices.
The latter is an a cappella, while the selected remix describes guitar stems;
keep the relationship explicit without inventing contribution details.

Keep concise song status and complete private `/now` credits for ordinary
listeners. Status text cannot substitute for notices. The pilot displays full
credits beside each audition, retaining source visibility; it is not blinded.
No UI implementation is supplied here. Safe link/text handling, complete bounded
pagination, expiry and caller-bound access still require implementation evidence.

## Proposed acquisition and storage routes

These plans are conditional on the finite run approval and listed practical
gaps, not new permission requests. No source API or audio endpoint was called
in this preparation. No account, credential, paid service or Discord session was used.

| Plan ID | Route after approval | Storage and current prerequisite |
| --- | --- | --- |
| jamendo-stream | Use the official registered application tracks API for these two IDs only, with `include=licenses musicinfo` and `audioformat=mp32`; use its stream field. Never use example media links or a download endpoint. | Transient operational buffering only; no offline library or replay cache. Application Client ID/access has not been supplied or checked. Revalidate identity/notices and the actual mp32 route before acquisition. The example's false download flag for JAM-1880336 is retained. |
| ccmixter-temporary-copy | Resolve the complete MP3 mix associated with each canonical page through the public page/query interface; select neither the sample archive nor the a cappella. | At most two bounded temporary complete copies, reused for the two listeners and deleted after the sessions, within 24 hours. Complete upstream notices/file selection first. No ongoing ccMixter-hosted playback loop. |
| fma-operator-hosted-import | Operator obtains each permitted complete file through the normal source download/import route once it is accessible, records its identity and notices, then auditions the operator-owned copy. | At most two imported files in an operator-owned evaluation volume, never in Git; retain for the approved session window, at most seven days. No FMA API, hotlink playback, forwarded listener searches, runtime scraping, or repeated fetch/play/delete. The 403 and unknown file details remain unresolved. |

These follow [Jamendo terms](https://devportal.jamendo.com/api_terms_of_use),
[ccMixter's access statement](https://ccmixter.org/about) and
[terms](https://ccmixter.org/terms), and the
[FMA app-developer policy](https://freemusicarchive.org/app-developers).
Readable FMA policy was already recorded in the
[source-research checkpoint](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6440870726);
this slice's direct access failure does not undo that evidence or the confirmed grant.
Public pages/indexes do not prove that any file can currently be retrieved.

For all routes, quarantine identity/notice conflicts, corrupt or incomplete
files; preserve safe failure metadata. Do not fetch an alternative encoding or
replacement recording automatically. Check published identity/notices before
each session; no scheduled recheck or background acquisition is proposed.
Delete expired/removed imports, retain their provenance record, and verify actual
removal. Audio and temporary buffers have no backups or exported listening clips.

## Finite run proposal

Optional study context: a non-commercial SobaFM selection audition by at most two
human reviewers using local headphones/speakers and complete visible credits.
This is a study-panel size, not a two-listener Discord audience limit. No
monetization; donations remain permitted under the existing confirmation. No
Discord playback or provider-generated audio. A single listener gives exploratory
evidence only. Name an evaluation/cleanup owner, reviewer, listener aliases,
equipment and a date window of at most seven days before execution.

Freeze the reviewed inventory commit, final credit snapshots and practical
access decisions before any audio attempt. Authorize only candidates whose
listed prerequisites are complete; retain all six slots, including blocked ones.
Revisit this proposal only if a future listening study is requested, reusing the
existing grants. No run is authorized by merging these files.

| Limit | Proposed maximum and accounting |
| --- | --- |
| Metadata completion | 16 initial page/API requests plus at most 4 retries total; only nominated identities, their known credit chain and applicable policy/format pages. No catalog pagination or replacement discovery. |
| Metadata response / total | 1,000,000 bytes per response; 20,000,000 bytes total including failed/retried responses. Stop before exceeding either. Retain selected facts, not raw page dumps. |
| Metadata effort | 30 active minutes. Unresolved pages/notices remain blocked at the bound. |
| Audio selections | Six fixed identities, two per source; at most two human listeners. Stem/a cappella evidence records are excluded. |
| Audio attempts | Jamendo: 4 initial streams (2 recordings × 2 listeners), each with at most 1 retry. ccMixter/FMA: 4 initial acquisitions total, each with at most 1 retry; reuse permitted local copies. At most 16 attempts in total. |
| Audio transfer | 20,000,000 bytes per attempt, including incomplete transfers; 320,000,000 bytes total. Formats: Jamendo mp32 and complete ccMixter MP3; FMA MP3 only if actually offered. No transcode/download-format fallback. |
| Network and concurrency | One request/transfer at a time; 10-second connect, 20-second inactivity and 120-second total attempt deadlines; at most 3 redirects within an approved source route. Stop on authentication challenge, access denial, unexpected cost or unapproved host/route. |
| Retry policy | Only transient failure, after the source's required delay; no retry to bypass 403/login, no guessed credentials. If the delay exceeds the run window, retain the failed slot. Partial bytes count; release partial storage before retry. |
| Listening | Start at the beginning; stop at natural end or 180 seconds. At most 18 minutes per listener, 36 minutes total. No seek, overlap, loop, fade, pitch/tempo change or edited excerpt. Record actual heard seconds. |
| Elapsed run | At most 120 active execution minutes across the seven-day window, including metadata, acquisition and scoring; pause counts do not create extra request/byte capacity. |
| Audio storage | At most 100,000,000 bytes total: two ccMixter copies, two FMA imports and one transient Jamendo buffer, each at most 20,000,000 bytes. Include partial/quarantined data in the cap; no concurrent replacement copy. |
| Audio retention | Jamendo buffer removed on stop/completion/failure; ccMixter copies removed after both sessions and within 24 hours; FMA imports removed after the final approved session and within seven days. Failed partial copies removed before another transfer. |
| Evidence and artifacts | At most 5,000,000 new bytes of selected metadata, ratings and safe logs; no audio in repository/logs. Keep accepted compact evidence in Git; remove temporary raw diagnostics within seven days. |
| Development storage | Existing agreement: at most 10 GB new project development storage; pause at 8 GB or system free space below 25 GB. Retained artifacts at most 500 MB. Re-measure before audio; the preparation checkpoint had approximately 95.56 GB free. These are separate from runtime RAM limits. |

The inventory preparation itself has a **10,000,000-byte** docs/static-data/log
allocation and uses no new environment or dependencies. The proposed audio
budget above belongs to the later run and is not spent in this slice.

Use the existing [candidate](candidate-template.md) and
[comparison](comparison-template.md) evidence fields for attempt outcomes,
heard duration, technical defects and separate enjoyment/fit/fidelity ratings.
Label the pilot records by candidate/request and `preliminary-pilot`, not J0/J1/C1;
do not populate the 48-cell comparison ledger with these exploratory observations.
First listener order: JAM-1848357, CCM-17241, FMA-NIGHT-OWL, JAM-1880336,
CCM-70993, FMA-GYMNOPEDIE-1. Second listener reverses that order. Keep ratings
independent and record source visibility, volume/equipment and any gain change.

Report readiness, attempts, failures, heard counts and ratings per source with
denominators of two nominated recordings and actual listeners. Unknown or
blocked cases remain missing, not zero-rated. Keep first-attempt failures and
do not replace a disliked/unavailable selection. A future pilot may motivate
additional comparison or repair specific credit/access gaps. No material
improvement claim is possible from these six nonrandom identities. This unrun
pilot is not a gate for #6; its current acceptance is tested prompt selection,
honest coverage and admission boundaries, aligned documentation and review.
