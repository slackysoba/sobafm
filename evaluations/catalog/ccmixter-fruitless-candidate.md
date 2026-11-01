# Complete Fruitless mix: separate ccMixter proof case

Status: **metadata prepared; availability unknown; admission not granted**.
Issue [#27](https://github.com/slackysoba/sobafm/issues/27) implements this case
after independent acceptance of the
[preparation checkpoint](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6552135455).
Observation: **2026-10-31 America/Los_Angeles**, **2026-11-01 UTC**.
This record authorizes no source, decoder, listening or Discord execution.

## Recording and supplied credits

The candidate is **[Fruitless by Dan_Mantau](https://ccmixter.org/files/Dan_Mantau/70072)**,
upload **70072**, complete-mix file **125900**, `Dan_Mantau_-_Fruitless.mp3`.
The source reports **8,232,794 bytes**, **3:26 / 206 seconds**, audio/mpeg,
CBR, stereo, 44k. These are reported metadata, not measured file properties.
The page's complete mix, country-rock description and separate named stems
support this selection; duration or the `remix` tag alone does not establish it.

| Relationship | Supplied names and evidence | Playback selection |
| --- | --- | --- |
| Mix 70072, 2025 | Dan_Mantau; featuring Lisa DeBenedictis; country-rock description and thanks for the a cappella | File 125900 only |
| Mix files 125901–125907 | Drums, Bass, Guitar 1, Guitar 2, Lead guitar, Rhodes, Cello; each reported 8,278,770 bytes / 3:27 | Excluded stems |
| Parent 25112, 2010 | [Fruitless by DeBenedictis](https://ccmixter.org/files/lisadb/25112), uploader lisadb; source page linked by the mix | Credit evidence only; Vox/Guitar files 29325/29326 excluded |

The supplied chain is **70072 → 25112**. The root page contains no further
“Uses samples from” section. Its info response omits `remix_parents`; that
omission alone is not evidence of a complete chain. The full page observation
bounds this review. Preserve **Lisa DeBenedictis** and **DeBenedictis** as supplied;
no separate composer or composition identity was provided.

Both pages supply **CC BY-NC 3.0 Unported**. The
[prepared catalog](ccmixter-fruitless-candidate.json) preserves nine notice
paragraphs, source links, the complete supplied chain, license/warranty terms,
and the distinction between reported metadata and unobserved file properties.
No additional recording-specific copyright or warranty notice was observed on
either page. Embedded file notices remain unobserved.
The existing [maintainer permission context](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441099250)
requires attribution and no commercialization or monetization, while permitting
donations. It does not grant a new execution window or catalog admission.

## Evidence and immutable identity

The [dated selected evidence](ccmixter-fruitless-evidence-2026-10-31.yaml)
records public routes, raw/comparison hashes, sizes and the existing permission
and policy references. The four-request metadata refresh ran from
**02:01:45.487840 to 02:01:53.159516 UTC**, totaling **100,563 bytes**,
with zero redirects, retries or media requests. Both info routes returned
HTTP 200 with `text/plain`. Exact reconstructed bodies were independently
verified before redundant capture copies were removed; frozen preparation
evidence remains separate. No private operational paths or session identifiers
are included in this repository.

The terms/about evidence is explicitly reused from the
[accepted policy checkpoint](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6551592128),
not represented as a fresh capture. Both refreshed HTML pages matched the
accepted bodies except the already permitted hidden decimal marker; both info
bodies were byte-identical to the retained source responses.

- Case: `fruitless-70072-125900`.
- Catalog file SHA-256: `2da224dd5d1c16d8d00d8a88e920d0f6052488b21128c2fef315f95f50b96dbc`.
- Recording metadata digest: `f7caef9aee0aed124b2004d4cade992627e20f5c45e52847d3214401665f8623`.
- [Observed projection](import-ccmixter-fruitless-observed.json) and
  [import context](import-ccmixter-fruitless-context.json) select only file 125900.

The original [WILD WINDSTEPS candidate](ccmixter-playback-candidate.md), catalog
and source evidence remain independent. Its later media attempt returned
[HTTP 403 with zero audio bytes](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6551972107).
That recording-specific access blocker has no established cause and is not
erased by preparing Fruitless. No automatic fallback or retry is implemented.

## Finite source boundary and remaining work

The [source proof](source-proof.md) binds this candidate before resource creation
and checks exactly six routes: mix page 70072, root page 25112, single-ID info
70072, single-ID root info 25112, terms and about. HTML uses only the accepted
`ccmixter-hidden-index-v1` normalization; both JSON bodies remain byte-exact.
Unique file IDs, source ownership, complete-mix label/filename, format, route
and numeric size are validated independently of array order. Upstream credits
are independently checked and never converted into playback eligibility.

A separately reviewed source run may create one temporary ccMixter copy:
six metadata requests / 3 MB, one media attempt / 10 MB, 600 seconds total,
the original three-second cleanup deadline, 100 KB evidence and one-hour
absolute retention backstop. There are zero retries and redirects. Delete on
completion, failure or cancellation; retain ownership and cleanup debt until
actual release. No library, backup or repeated playback loop is proposed.

The **206-second** reported duration exceeds the existing decoder experiment's
**150-second / 28,800,000-byte PCM** fixture. A future finite decode proposal
must account for that separately. Issue #27 changes no decoder bound or native
implementation and runs no source/audio request. Transport, full-file integrity,
embedded notices, decoding and audible drain remain unqualified. Ordinary
selection continues to reject this candidate without availability evidence and
a separate trusted admission review.
