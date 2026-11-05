# 0006: Best-fit `/play` matching with Gemini

- Status: Accepted
- Date: 2026-11-05
- Scope: [issue #105](https://github.com/slackysoba/sobafm/issues/105)
- Authority: maintainer direction recorded on that issue
- Supersedes: request interpretation and ranking in [decision 0002](0002-prompt-catalog-selection.md). Its three-source pool, complete-recording rule, trusted admission reviews, immutable credits and source rules stand.

## Problem

`/play` matched requests with a fixed phrase table and 32 labels. Offline runs showed that most free-form requests were refused, and that words outside the table were silently dropped from requests that did match. The maintainer's intent is different: "someone can type in something crazy, and gemeni will still interpret the request, and find the best fit from the music sources we're using, by matching the request to whatever information about the song is available."

## Options

1. **Gemini fills in the existing query model.** Small change, and the model never sees song data. Requests are still reduced to 32 labels, and songs are still described only by those labels, so unusual requests lose what made them unusual.
2. **Embeddings or an audio-analysis index.** This could match on sound, but adds dependencies, ingestion compute and storage, and needs a bounded comparison first under the requirements. It is also more than the 90 MB service limit comfortably allows at runtime.
3. **Live search of each source.** FMA has no open API and doesn't allow hotlinking, and first audio would depend on three external services.
4. **One Gemini call that ranks the prepared catalog.** The model reads the request and what is known about each admitted recording, and returns a ranked list.

## Decision

Use option 4.

- **One call per `/play`.** No retries, agent conversation or tools. The ranked list is the program's plan, so `/skip` and continuation need no second call.
- **Best fit, disclosed.** A musical request always gets the nearest admitted recording, and the reply says whether it is the named recording, a close match or just the nearest available. This includes a specific song the catalog doesn't have. It replaces "return no match" for style and identity requests.
- **Authority stays local.** The model names recordings only by position in the list it was sent. Output that doesn't exactly match the schema is rejected whole. Admission, credits and media checks still run locally, so a bad answer can only choose a different admitted recording.
- **Constraints become judgment.** Duration, exclusions and "instrumental" are read by the model rather than enforced as hard filters. A wrong call plays an admitted recording that fits worse, and the user can replace it.
- **Songs keep their source information.** Snapshots gain optional `details`: tags, tempo, album and similar source text. They are matching hints, not admitted facts, and stay outside the review digest. Existing reviews stay valid, and details can't grant admission or change credits.
- **What leaves the service.** The request text, each admitted recording's title, creators, length, labels and details, and a fixed instruction. No Discord identities, credits or credentials in the prompt. `store` is off on every call.
- **Failure preserves audio.** Timeouts, provider errors and invalid output leave current music unchanged and explain the problem. There is no phrase-table fallback, because the requirements forbid unvalidated search paths and two matchers would behave differently without warning. Requests the model judges unrelated to music also change nothing.
- **API and dependency.** Use the Interactions API with `gemini-3.5-flash-lite`, its documented stable fast model, as of 2026-11-05. The documentation calls this API the recommended standard for new projects and calls `generateContent` legacy. Call it over the `aiohttp` already in the lock rather than adding the Google SDK and its dependency tree under the [ADR 0004](0004-amd64-whole-service-adoption.md) limits. The response read, the song list and the time per call are bounded.
- **Replacement, not addition.** The phrase table, query model and local ranking are removed.

## Consequences

- The per-request cost grows with the catalog, since every call sends it. The song list is capped at 512,000 characters and refused at startup if larger. At the documented $0.30 per million input tokens, a catalog of 768 short entries is on the order of one or two US cents per request; the free tier costs nothing but allows Google to use the content to improve its products.
- First audio now waits on a provider call, bounded at 10 seconds. It counts against the 15-second median latency target and hasn't been measured.
- A program lasts at most the 20 ranked recordings, then ends with the existing explanation instead of repeating one. That is roughly an hour at typical lengths. Going further would need a second call or a longer list, which is a later choice.
- The model reads descriptions and can't hear the music, so recordings with sparse metadata will match poorly.
- A small catalog limits how good any best fit can be. Growing the real catalog is separate work and remains the largest gap in match quality.
- Metadata goes into the prompt as untrusted data, and a hostile tag could steer a choice. Doing so can only select another admitted recording. Replies use fixed wording and catalog text, not model text.
- Member cooldown settings remain Outcome 4 work, so until then cost exposure is limited by one in-flight match per server and the voice-channel requirement.

## Verification needed

Offline tests establish the request shape, validation, failure handling and state safety. They don't establish that the live API accepts this request, that the documented response path is right or that the matches are good. An [opt-in evaluation](../../evaluations/gemini_matching/README.md) checks those with a maintainer-supplied key and go-ahead. Re-check after any change to the model or API revision.
