# Observed FMA Night Owl metadata

Status: **web metadata prepared; operator original supplied; no project audio copy or admission**.
Issue [#31](https://github.com/slackysoba/sobafm/issues/31). Observed
**2026-10-31 America/Los_Angeles** (2026-11-01 UTC). This dated follow-up
resolves the web-notice and license gaps for Night Owl in the unchanged
[2026-10-27 inventory](three-source-inventory.json); it does not replace that
historical observation or qualify another FMA recording.

## Recording and supplied credits

[Night Owl by Broke For Free](https://freemusicarchive.org/music/Broke_For_Free/Directionless_EP/Broke_For_Free_-_Directionless_EP_-_01_Night_Owl/)
is the first recording on **Directionless EP**, released January 18, 2011,
with a reported duration of **3:14 / 194 seconds**. The canonical recording
page path is its catalog identity; no numeric ID or source file is invented.
The recording and album pages identify a released EP recording rather than a
stem, sample or preview. This supports the manifest's `complete` kind as web
metadata only; actual file completeness and duration remain unverified.

The recording page and ordinary download dialog link to
[CC BY 3.0 Unported](https://creativecommons.org/licenses/by/3.0/).
The dialog requires song title, artist name, source **Free Music Archive** and
license type **CC BY**. The structured catalog preserves the recording and
license backlinks, artist role, album/release date and attribution obligation.
Composer and embedded audio notices remain unknown; no audio was modified.

The recording, artist overview and linked FMA biography were captured completely
without truncation or keyword cuts: **204,508 response bytes / 57,010 selected
evidence bytes** across three requests. Their visible notice sections supplied
no further recording contributors or attribution terms. This conclusion does
not cover unvisited external pages or embedded tags. Unrelated comments and
website navigation are excluded from the public projection.

Track info says **Instrumental: No** and **AI generated?: No**. Preserve these
source claims without inferring actual vocal content. The artist's generic
biography does not override the recording field. `Electronic` maps to the
existing weak descriptor; `Minimal Electronic` remains an explicitly unmapped
source tag. No taxonomy or importer behavior changes in this slice.

## Source access and storage

The [included evidence record](fma-night-owl-evidence.json) contains the six
recording/artist/album/policy/dialog observation URLs, UTC times, response hashes,
capture hashes and scope. Its `permission`, `hosting_policy` and
`head_observation` sections resolve the companion references in the manifest.
Raw HTML, public comments, private locations, credentials and media URLs are
not included. Retained capture hashes identify reviewed evidence; they do not
claim that the compact public projection can reconstruct the original HTML.

Reuse the [confirmed source permission](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441099250):
attribution, no commercialization or monetization, donations permitted.
The observed [FMA app-developer policy](https://freemusicarchive.org/app-developers)
requires permitted copies to be operator-hosted; direct FMA playback/hotlinking
is disallowed. This work adds no live search, acquisition or hosting service.

At **2026-11-01 03:06:42 UTC**, the single
[released HEAD inspection](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6552671365)
returned **302**, `text/html`, with Location `https://freemusicarchive.org/login`.
No audio MIME or file length was advertised. No body was read, redirect followed,
retry attempted or GET fallback made; the command owner released with no cleanup
debt. The [public outcome](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6552729791)
records a consumed attempt and **unestablished anonymous audio access**.
The later [accepted single GET](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6556442342)
at **2026-11-01 09:56:51.809867 UTC** also returned 302, `text/html`, redirecting
to login. No body API was called or plaintext body bytes delivered; kernel/TLS
buffering is not claimed zero. No redirect, retry, authentication or fallback
followed. The child and reader/pipes settled with no debt. Its cleanup clock never
started, so this establishes no three-second cleanup bound. Anonymous audio
access remains unestablished; this is not provider-wide unavailability. The dated
catalog/evidence snapshot above remains unchanged.

Accepted preparation for one operator-hosted evaluation copy is limited to
**20,000,000 bytes / seven days**, with exact file identity/integrity, acquisition
evidence, absolute expiry and accountable cleanup ownership required before
intake. No project copy or quarantine has been created. This acceptance releases
no import, playback or decoder-case expansion. The source's reported **194 seconds
of audio** is distinct from the existing decoder case's **150-second operation
wall-clock deadline**; duration alone establishes neither fit nor incompatibility.
All existing PCM limits, native bounds and proof results remain unchanged.

Issue [#40](https://github.com/slackysoba/sobafm/issues/40) supplies an opt-in
[bounded local intake owner](fma-local-intake.md), tested with synthetic files.
The maintainer supplied an original on **2026-11-02**, reporting acquisition just
before supplying it and an artist-attribution download notice. The exact time is
unknown and the notice is a maintainer-reported paraphrase, not an independently
captured literal upstream notice. Read-only preparation recorded **8,054,458 bytes**
and SHA-256 `7952dce61b28f454ff3ee0183f85df52b2a9f2edfcd6a0ebafc12f981f666ed5`;
its location remains private. No format, complete-file or embedded-notice review
is implied. Keep complete credits through `/now`; status remains best effort.

Issue [#42](https://github.com/slackysoba/sobafm/issues/42) binds a neutral retention
anchor, exact/lower-bound basis and bounded operator evidence in v2 intake records,
and keeps one seek/stream validation consumer owned through actual cleanup. The
conservative anchor **2026-11-02T00:00:00Z** permits expiry no later than
**2026-11-09T00:00:00Z**; it is not an actual timestamp claim. Keep only the original
until a concrete consumer and finite source/image-bound execution are reviewed.
A successful pending copy or byte-stream verification creates no admission review,
access-ready claim or availability promotion. The dated catalog digest is unchanged.

## Reproduce the unadmitted snapshot

Issue [#44](https://github.com/slackysoba/sobafm/issues/44) now supplies the
[concrete validation source and packet contract](../fma_validation/README.md).
Its synthetic tests do not inspect this recording. Fresh topology/native-filter
qualification, exact source/image/target/window review and storage allocation
remain required before actual intake. The supplied file's tag size is unknown;
the supported Mutagen path explicitly rejects tags above 512 KiB within its
1 MiB absolute framing cap. No actual embedded-credit or decode verdict is claimed,
and this catalog remains unchanged and unadmitted.

Use the unchanged importer with the
[observed manifest](import-fma-night-owl-observed.json) and
[dated context](import-fma-night-owl-context.json):

```sh
uv run python -m sobafm.catalog_import fma \
  --input evaluations/catalog/import-fma-night-owl-observed.json \
  --context evaluations/catalog/import-fma-night-owl-context.json \
  > night-owl-catalog.json 2> night-owl-import-report.json
cmp night-owl-catalog.json evaluations/catalog/fma-night-owl-catalog.json
cmp night-owl-import-report.json evaluations/catalog/fma-night-owl-import-report.json
```

The [catalog](fma-night-owl-catalog.json) and
[report](fma-night-owl-import-report.json) are exact CLI outputs. The evidence
record binds their hashes and the parsed metadata digest. A focused offline
regression reproduces both outputs, preserves the supplied credits and unknowns,
and confirms rejection without a separate trusted admission review.

The one-record FMA partition is a complete **bounded metadata subset**, not the
provider's catalog. Jamendo and ccMixter remain missing in this isolated input.
Availability stays **unknown**, no vocal fact is invented, and the importer
creates **zero admission reviews**. Normalization, a claimed operator copy or
complete web notices cannot establish file integrity, embedded credits, access
readiness, music quality or playback qualification. A later trusted review must
bind the actual completed evidence to the exact current metadata digest.
