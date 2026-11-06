# Provider access, privacy and free-tier limits

Checked on **2026-11-06** against the official documentation linked below. Recheck these sources at build time and before changing the audience, account, model or API. This guide records the current path and its limits; it does not authorize a live call, accept terms for an operator or qualify the release.

## What is available in SobaFM

| Capability | Current state | Provider path |
| --- | --- | --- |
| Catalog `/play` and automatic refill | Implemented. Gemini ranks admitted local recordings; it does not supply their audio or search music services. | `gemini-3.5-flash-lite` through the Gemini Interactions API, `v1beta`, with SobaFM's pinned `Api-Revision: 2026-05-20` and `store=false`. See [ADR 0006](decisions/0006-gemini-play-matching.md) and [ADR 0011](decisions/0011-program-refill.md). |
| Generated-plan interpretation | Implemented as a library, with no Discord caller. | The same Gemini client interprets a musical request and optional current generated plan. See [architecture](architecture.md#generated-plan-interpretation-not-yet-wired-in). |
| Generated music | The Lyria transport library is under review in [#128 / PR #136](https://github.com/slackysoba/sobafm/pull/136). Generated command integration is not present: there is no available `/freestyle` command. | The maintainer selected free-tier `lyria-realtime-exp`. Free-tier access has not been confirmed. |

Google lists `gemini-3.5-flash-lite` as a stable model and supports it on [Interactions](https://aistudio.google.com/docs/interactions#supported-models-agents). The [model list](https://aistudio.google.com/docs/models) and API documentation establish published support, not the access or quota of a particular project. SobaFM's revision pin is an implementation choice, not a claim that every future API revision works.

Google documents Lyria RealTime as an instrumental streaming model, using a persistent WebSocket connection and weighted text prompts. Its [guide](https://aistudio.google.com/docs/realtime-music-generation) names `models/lyria-realtime-exp`; the [Live Music reference](https://aistudio.google.com/api-reference/live-music/bidi-generate-music) documents the `v1beta` protocol and calls the API preview. The model is experimental: Google says [experimental endpoints](https://aistudio.google.com/docs/models#experimental) can change availability and usually are unsuitable for production. There is no approved fallback if this model becomes unavailable.

## Account access and audience eligibility

Gemini requires authentication with an API key associated with a project; Google's [key guide](https://aistudio.google.com/docs/api-key) describes the current key types and access rules. For the implemented catalog path, the operator supplies `SOBAFM_GEMINI_API_KEY` as described in [operations](operations.md#install-and-configure). Keep it out of prompts, chat, logs and shared diagnostics. This guide adds no generated-mode configuration or account changes.

An account that can open AI Studio or make a request does not establish that a deployment's audience is permitted. The [Gemini API Additional Terms](https://aistudio.google.com/docs/terms), effective March 23, 2026, require all of the following:

- The API user must be at least 18. An API client must not be directed at, or likely to be accessed by, people under 18. This applies to catalog matching as well as generation. A Discord channel membership check is not age verification. Google's separate [AI Studio account guidance](https://aistudio.google.com/docs/troubleshoot-ai-studio#age-requirements) covers account age and verification.
- Access and the API client's users must be in [available regions](https://aistudio.google.com/docs/available-regions). **An unpaid API client cannot be offered to users in the EEA, Switzerland or the UK.** The terms require Paid Services for those users, even where the region list permits account access. SobaFM's free-tier choice does not authorize a paid deployment to serve them.
- The service is for developers building for professional or business purposes, rather than consumer use. The maintainer's [recorded judgment](https://github.com/slackysoba/sobafm/issues/128#issuecomment-6681187335) is: "the project falls within Gemini's terms of use, but it is not a commercial project. It is a community project". That is the project's decision, not an assurance from Google or a general exemption for community projects. It does not establish any individual listener's age or location.

Provider access is also separate from permission to play a recording. The catalog's admission, attribution, non-commercial use and source-specific storage rules remain in [requirements](requirements.md#music-selection-and-catalog-preparation) and the accepted catalog decisions. A Gemini answer grants no recording rights.

## What leaves the service

For catalog matching, SobaFM sends the listener's request, a fixed instruction and descriptive metadata for admitted recordings: titles, creators, composition/composer where present, durations, labels and source details such as tags, tempo, album and notes. Refills send the same request with only the remaining recordings. The model returns positions in that list and a fit assessment; SobaFM validates the output before it changes the program. It sends no catalog audio, Discord identities, credit snapshots or credentials in the prompt. The API key authenticates the request in a header. See [ADR 0006](decisions/0006-gemini-play-matching.md), as amended by [ADR 0011](decisions/0011-program-refill.md).

The generated-plan library sends the request, a fixed instruction and, when supplied, the current generated plan as musical context through the same client. It accepts no Discord identity. The selected Lyria path would send weighted musical directions, generation settings and playback controls, and receive generated audio, as described in the [Live Music protocol](https://aistudio.google.com/api-reference/live-music/bidi-generate-music). Neither path calls for listener voice input. This describes the intended generation data flow, not an integrated command. The plan library's failure fallback can use the request as written, so operators must not assume interpretation removes sensitive text.

Omitting identity fields does not anonymize arbitrary request text or catalog notes. Tell listeners that their requests go to Google, including again during refill, and keep personal, sensitive and confidential information out of requests and supplied metadata. Google's [unpaid data terms](https://aistudio.google.com/docs/terms#data-use-unpaid) expressly say not to submit such information.

## Storage and Google's use of data

**Unpaid prompts and output may be used to improve Google's products, with human review.** The [unpaid data terms](https://aistudio.google.com/docs/terms#data-use-unpaid) cover submitted content and generated responses. Reviewers may read, annotate and process input and output; Google says it disconnects this material from the account, API key and project before review. This applies to each catalog refill as well as an initial request, and to free-tier generated music if that access is available.

SobaFM sets `store=false` on every Interactions call. Google's [storage documentation](https://aistudio.google.com/docs/interactions#data-storage-retention) says this opts out of storing the interaction object for retrieval and server-side conversation state; otherwise the default retention is one day on the free tier and 55 days on the paid tier. **It does not opt out of the unpaid data-use terms or establish zero retention by Google.** The [Live Music schema](https://aistudio.google.com/api-reference/live-music) documents no equivalent `store` switch or retention period; do not transfer the Interactions setting or its retention periods to Lyria.

Two distinctions in the [terms](https://aistudio.google.com/docs/terms#data-use-paid) matter:

- For users in the EEA, Switzerland or the UK, the paid-services **data-use terms** apply even to unpaid quota. That does not remove the separate restriction on offering an unpaid API client to users there.
- Paid-services data terms say prompts and responses are not used to improve products, but permit limited logging for abuse prevention and required disclosures. The terms define Gemini API paid access by the project's active billing association; AI Studio has its own account-based definition. Neither opening AI Studio nor an API key alone proves which data terms apply. These distinctions are information, not a recommendation or authorization to enable billing.

Locally, matching and plan interpretation log safe outcomes, counts, timing and failure kinds, not request or response text. Current durable application state is server settings and remembered voice channels, not a prompt history. Catalog metadata and permitted imported audio remain in the operator's catalog volume. The requirements limit generated playback to transient storage; no generated-audio archive or replay cache is promised. See [architecture](architecture.md) and [operations](operations.md#install-and-configure). Local storage choices do not control Google's processing.

## Free-tier cost and limits

The maintainer's [generation decision](https://github.com/slackysoba/sobafm/issues/128#issuecomment-6681208164) is **"Free-tier Lyria RealTime"**. Free tier means **no spend**: no Prepay, billing activation, paid overage, credit purchase or paid fallback is authorized. Google's checked [pricing page](https://aistudio.google.com/docs/pricing) does not list `lyria-realtime-exp`, and the checked RealTime guide does not promise free-tier access or publish its quota. Published protocol documentation does not establish availability for this account. If the free tier cannot serve it, return the decision to the maintainer; do not switch model or payment tier.

For catalog matching, the [Gemini 3.5 Flash-Lite pricing table](https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite) lists input and output as free of charge on the free tier. Free does not mean unlimited. Google's [rate-limit documentation](https://aistudio.google.com/docs/rate-limits) says limits depend on model and project tier, apply per project rather than per key, and can constrain requests and tokens separately. It directs operators to AI Studio for their active limits. No account quota was inspected for this guide; another key in the same project is not a separate allowance.

Catalog programs make further calls without a fresh listener command. [ADR 0011](decisions/0011-program-refill.md) sets the bounds:

- Ask for eight recordings initially and when the last recording on the current list starts. Accept a shorter valid answer; leave out all recordings already played or tried. Make no call once none remain.
- At most three calls per refill, with a 15-second wait and then up to 60 seconds. The second wait shortens using the heard track's remaining duration to leave 16 seconds for the final call and pre-roll, but never falls below 15 seconds. A refusal, error or answer with nothing new consumes an attempt.
- Each call has a 10-second timeout, a 64,000-byte response limit and a 2,048-output-token cap. There is at most one refill in flight per program and four model calls across the service. The catalog description is capped at 512,000 characters.
- The accepted bound is no more than three calls per admitted recording per program. A shorter answer or retry increases calls; eight songs per answer is a request to the model, not a measured guarantee. After the final failed refill attempt, the current recording finishes and the program ends, or it ends immediately if already silent. It does not repeat the catalog automatically.

These are application bounds, not a reservation of provider quota. Refills resend request and catalog text and consume quota without taking the member change cooldown. An initial matching failure preserves current music; refill exhaustion ends that program. Stopping, leaving, replacement by a new program, listener-grace expiry, voice loss and shutdown cancel refill work, and stale results are discarded. The same free-tier data-use disclosure applies throughout.

## Facts still needing an authorized live check

This documentation change made no model, music-source, Discord or voice request. Before any live check, obtain the maintainer's explicit go-ahead on its issue. For Lyria, #128's fixes, record update, review and merge come first. The remaining facts include:

- Whether the chosen account's free tier actually serves `lyria-realtime-exp`, and its effective limits and availability.
- Whether the pinned API requests and response handling work at the current head; whether Gemini returns eight suitable recordings across refills, and the actual token usage, latency and rate-limit behavior.
- Lyria's actual stream format and session behavior, renewal and failure handling, prompt filtering, continuity, request fit and whether optional vocalizations stay wordless. The [guide](https://aistudio.google.com/docs/realtime-music-generation#limitations) describes instrumental output and safety filtering, not SobaFM's measured results.
- Generated playback, transitions, cancellation, resource fit and release in the actual Linux amd64 image, followed by Discord integration once that command exists. Offline fakes cannot establish those facts or the [release criteria](requirements.md#acceptance-and-evidence).

This is a list of unresolved facts, not a live-test procedure or permission to run one. A successful API request would not by itself establish terms compliance, continued availability, music quality or release readiness.
