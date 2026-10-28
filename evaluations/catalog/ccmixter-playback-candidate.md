# One ccMixter candidate for the first playback slice

Status: **source metadata prepared; file validation and admission pending**.
Issue [#16](https://github.com/slackysoba/sobafm/issues/16); observation date
**2026-10-27 America/Los_Angeles** (2026-10-28 UTC). Prepared by the #16
implementation worker for coordinator review. No audio was requested, acquired,
decoded, played or rated. No playback source is enabled by this record.

## Candidate and complete supplied credits

Use **[WILD WINDSTEPS by Ant.Survila, featuring Dan_Mantau](https://ccmixter.org/files/AntSurvila/70993)**,
ccMixter upload **70993**, file **129564**. The public `info` response identifies
one file, `AntSurvila_-_WILD_WINDSTEPS.mp3`, as `audio/mpeg`, VBR, stereo, 44 kHz,
**2:09** and **4,693,829 bytes**. All are reported metadata, not measurements.
The publisher's guitar-stem description and the editor's description of an
assembled instrumental support classifying the published mix as a complete
recording. Neither duration, the editorial badge nor the `remix` tag alone
establishes completeness. Actual file content remains untested.

| Published relationship | Supplied creator and notices | Authoritative evidence |
| --- | --- | --- |
| Selected mix, 70993 | WILD WINDSTEPS; Ant.Survila, uploader AntSurvila; featuring Dan_Mantau; 2026; guitar-stem description | [Mix page](https://ccmixter.org/files/AntSurvila/70993) and single-ID `info` response |
| Direct source, 70072 | Fruitless; Dan_Mantau; featuring Lisa DeBenedictis; 2025; country-rock description and thanks for the a cappella | [Dan_Mantau page](https://ccmixter.org/files/Dan_Mantau/70072) and single-ID `info` response |
| Parent's source, 25112 | Fruitless; displayed DeBenedictis, uploader lisadb; 2010; a cappella | [DeBenedictis page](https://ccmixter.org/files/lisadb/25112) and returned `info` record |

All three pages identify **Attribution Noncommercial (3.0)** and link to
[CC BY-NC 3.0 Unported](https://creativecommons.org/licenses/by-nc/3.0/).
The source API uses HTTP for this official license link; the catalog uses HTTPS,
as the existing importer does. No additional recording-specific copyright or
warranty notice was observed. License/warranty terms remain linked in full.
The final source page has no further **Uses samples from** section. Its API
omits `remix_parents`; that omission alone is not evidence of an empty chain.
This resolves the **published supplied chain**, not undisclosed authorship.

Preserve Lisa DeBenedictis and the displayed DeBenedictis alias together. The
selected instrumental describes guitar stems; the retained upstream credit
does not assert that her voice occurs in it. Do not substitute either Fruitless
source, stem or a cappella for the selected mix. No separate composer or
composition identity is supplied for this mix; those catalog fields stay null.

The [prepared existing-schema catalog](ccmixter-playback-candidate.json) contains
the title, all supplied names, three source backlinks, license links, supplied
descriptions, relationship explanation and unchanged-audio notice. Its eleven
notice paragraphs fit the current 16-paragraph/1,024-character bounds. The
catalog parser verifies text/link bounds; ordinary-listener `/now`, escaping,
pagination and expiry are still unimplemented and unqualified. Never truncate
these notices to make a status message fit.

## Permission and actual public access route

Reuse the maintainer's [confirmed three-source grants and conditions](../../docs/catalog-evaluation.md#confirmed-three-source-permission):
attribution, no commercialization or monetization, donations permitted. Do not
reopen the grant or deferred human listening. The published license is retained
alongside that authority. [CC BY-NC 3.0 sections 3–4](https://creativecommons.org/licenses/by-nc/3.0/legalcode)
cover reproduction, performance and the associated attribution/noncommercial
conditions; preserve notices and identify actual technical changes.

The current [ccMixter access statement](https://ccmixter.org/about) explicitly
permits third-party Query API use and describes licensed downloads. The
[terms, sections 4, 7 and 8](https://ccmixter.org/terms) require recording-specific
license compliance and notices, prohibit overburdening/unauthorized access and
apply to API use. Its incorporated [privacy policy](https://ccmixter.org/privacy)
describes usage logging/analytics. This finite unauthenticated lookup sends no
listener identity or prompt. No new account, credential or paid service is used.

The [documented Query API](https://ccmixter.org/query-api) is called at
`https://ccmixter.org/api/query` with `f=json`, `dataview=info`, `ids=70993`,
`limit=1`. Its selected file's `download_url` was an ordinary HTTPS URL on
**ccmixter.org**, path
`/content/AntSurvila/AntSurvila_-_WILD_WINDSTEPS.mp3`, without query, fragment
or credentials. This is an observed route, not a guessed URL or a proven
successful media response. No HEAD, GET or range request was made to that path.
Keep the path here as evidence; the catalog and import fixtures contain no
media URL. Re-resolve the same file identity at execution, not a permanent
signed URL or a host-wide stream loop.

## Existing importer and explicit preparation

[The input projection](import-ccmixter-playback-observed.json) retains selected
identity, license, tag, file identity/format/duration and direct-parent fields
from three real responses. [Its context](import-ccmixter-playback-context.json)
nominates the three IDs and selects file 129564 only for 70993. Other MP3 entries
are retained as upstream metadata; they are not acquisition candidates. Remote
URLs, server paths, hashes, review text and unrelated user data are omitted.
No parent license was invented inside the raw relationship objects.

Use the existing importer without a new adapter:

```sh
uv run python -m sobafm.catalog_import ccmixter \
  --input evaluations/catalog/import-ccmixter-playback-observed.json \
  --context evaluations/catalog/import-ccmixter-playback-context.json \
  > draft-catalog.json 2> import-report.json
```

Its result remains a **draft**: unknown kind/availability, direct-parent license
gaps, upstream notices needing review, and no generated admission. Multi-file
upstream records have no selected duration. Separately prepare the one-record
catalog by joining the three observed source records and page evidence:

1. Retain the mix's imported identity, reported duration, provenance and weak
   descriptor facts unchanged. Unsupported `synthesizer` remains unmapped.
2. Classify the published mix as complete using the page evidence above; omit
   upstream source recordings from the playback partition.
3. Preserve both upstream works, their licenses, all supplied creator names and
   aliases, descriptions and chain limitations in the dated credit snapshot.
4. Keep **availability unknown** and supply **no admission review**. The selector
   must still reject this prepared candidate. A later trusted reviewer must bind
   any actual approval to `metadata_digest(recording)` after access/file evidence
   and all notices, including any file-embedded notices, have been reconciled.

The new deterministic regression validates these boundaries, the imported file
identity, all three licenses and the prepared candidate's unchanged identity,
duration and facts. It does not contact ccMixter. Do not describe schema checks
or a complete one-record partition as full-catalog coverage or playback approval.

## Proposed finite source proof

This is the concrete handoff for **coordinator release**, not an execution grant
created by merging documentation. Assign one proof/cleanup owner and record the
exact candidate revision, execution commit, time window and storage checkpoint
on #16 or its playback follow-up before starting. Acquisition can be measured
separately; decoder execution requires #7's cleared exact binary/source route
and Linux resource setup. No human listening is required. No Discord operation,
LLM call, native build or new source/provider abstraction is part of this proof.

| Bound | Proposed one-run limit and behavior |
| --- | --- |
| Identity | One upload 70993, one file 129564, one unchanged MP3; no replacement song, archive, stem, fallback encoding or sample pack |
| Recheck | At most 6 metadata requests, each capped at 500,000 decoded bytes and 3,000,000 total: current three pages, single-ID mix info, terms and access statement. Compare the fixed chain, license and route; any change stops the run for review. No search/pagination. |
| Requests | One request at a time; 10-second connect, 20-second inactivity, 120-second absolute deadline per request; redirects disabled. Unexpected redirect, auth challenge, 403, host/path or notice conflict stops rather than finding a workaround. |
| Audio attempts | One full GET from the newly resolved exact route; at most one retry only for a transient transport/5xx failure. Respect Retry-After within the run window. No retry for identity, size, permission or content mismatch. Close/join and remove the first partial copy before retry. |
| Transfer bytes | At most 10,000,000 body bytes per attempt and 20,000,000 total, counting failed bodies. Enforce while reading, not only from Content-Length; reject an advertised excess before reading. Disable content encoding. The unchanged candidate expects 4,693,829 bytes; a discrepancy is unresolved and blocks success. |
| Storage | One operator-owned, private temporary run directory outside Git and release artifacts, with at most 10,000,000 audio bytes including partial/quarantined data. Reserve before acquisition; no concurrent replacement, replay library, backup or exported audio. |
| Decode validation | After the Linux gate, decode the complete acquired file locally to 48 kHz stereo signed 16-bit PCM and consume it incrementally without writing/retaining a complete PCM copy. At most one decoder, 60 seconds wall time and 28,800,000 processed PCM bytes (150 seconds). Keep current qualified worker/PCM/copy caps; this output-count bound is not a RAM allowance. |
| Completion | Require HTTP success and transport EOF; actual byte count matching current unchanged source metadata and Content-Length when supplied; local SHA-256; verified file/container identity and complete decode with natural EOF, no decode error and exit zero. Record PCM byte/sample count and final incomplete frame separately. Never pad/truncate to force success. |
| Duration | Expect approximately 129 seconds from metadata; report measured sample duration. A difference over 2 seconds stops automatic qualification for investigation. Closeness alone never proves completeness; ending, corruption and silence/clipping checks remain separately reported. |
| Credits | Inspect bounded file-embedded tags/notices during the permitted validation. A new required notice or conflict needs a new credit snapshot and review; do not approve the existing digest silently. Preserve source MP3 and technical-conversion notices. |
| Cleanup | One owner and original three-second cleanup deadline for cancellation, HTTP/decoder close and joins. Deadline failure retains cleanup debt/leases and blocks retry or further admission until actual release. Verify partial/full-file removal, not merely a requested deletion. |
| Retention | Delete the copy on completion/failure/cancellation, with a one-hour absolute backstop. If removal fails, report retained bytes/path privately, keep ownership and stop. Retain only compact non-audio evidence; no automatic backup. |
| Run/evidence | At most 10 active execution minutes in one window, at most 100,000 new evidence/log bytes; safe statuses, counts, hashes and timestamps only. No raw media, provider bodies, credentials or private paths in public evidence. |
| Development disk | Existing 10 GB project-development agreement, pause at 8 GB or C: free below 25 GB, retained-artifact cap 500 MB. Re-measure before execution. These caps are distinct from runtime memory. |

The owner reports transfer completion, decode completion, measured PCM,
notice reconciliation and actual cleanup as separate verdicts. Without playback,
**audible drain, enjoyment, fidelity and Discord delivery remain unobserved**.
A failed/incomplete verdict is useful evidence and must not be turned into a
successful admission. Full technical defect screening, Linux containment,
native arm64 qualification and the ordinary-listener credit UI retain their
separate requirements. Success here would support one fixed source-side proof,
not station reliability or a general temporary-download policy for other sources.

The [first source-proof component](source-proof.md) implements a narrower
transport-only evaluation with no retries. It requires separately reviewed
metadata fingerprints and explicit live invocation, and leaves file integrity,
decoding, embedded notices and admission pending. Its owned fixtures and local
tests do not constitute a live proof or release the execution gates above.

## Research accounting and preserved limitations

Only **three unique recording records** were examined for this preparation.
The earlier [Sunrise and WILD WINDSTEPS inventory](three-source-inventory.json)
is unchanged. Sunrise's historical upstream gap is not reopened or erased.
The earlier two-ID response missing 70993 remains valid historical evidence;
the current single-ID response now resolves 70993. A new two-ID request for
70072 and 25112 returned only 25112; the bounded single-ID 70072 request then
resolved it. These partial batches are not proof of source unavailability.

| Metered direct metadata request | Returned body bytes | Outcome |
| --- | --- | --- |
| `info`, `ids=70993`, `limit=1` | 3,624 | Exact mix and file identity |
| `info`, `ids=70072,25112`, `limit=2` | 7,475 | Only 25112 returned; missing 70072 retained |
| `info`, `ids=70072`, `limit=1` | 8,259 | Exact parent and root relationship |
| Three recording HTML pages | 40,242 + 38,784 + 46,043 | Complete public page reads |
| Terms / about / license / privacy | 58,016 + 37,139 + 53,062 + 43,546 | Current policy reads |
| **Total, ten direct requests** | **336,190** | No media body |

There were also **nine browser-tool opens/reopens** of the same official pages
and Query API documentation: **19 metadata operations total**, conservatively
counting cached rereads. That tool exposes parsed text but not fetched wire/body
sizes; the aggregate 5 MB network-byte budget therefore has a **measurement
gap**, not a certified pass. Direct reads were capped at 1,000,000 bytes for the
first API response and 500,000 for every later response. Research stopped with
the candidate resolved; no additional queries are planned. Future proof uses
only a directly metered client with explicit total accounting.

Preparation used no new environment, installation, build, cache or audio
artifact. Initial checkout was 660,696 bytes and C: had approximately 94.8 GB
free. The small raw observations remain private for independent review; only
selected metadata/notices are committed. Shared Git/cache/environment bytes
are counted once in the coordinator's storage record. Review and required
Linux check results belong on the PR, bound to its exact commit. The optional
listening study remains deferred, with no invented ratings.
