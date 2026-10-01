# ADR-0002: Interpret requests with Gemini structured output

- **Status:** Accepted
- **Date:** 2026-10-01
- **Issue:** #5

## Context

Members describe music in free text: moods, genres, scenes, references to artists or games, relative changes such as "faster", and sometimes requests that are not about music at all. Lyria RealTime works best with short, descriptive weighted prompts and numeric settings for tempo, key, density, and brightness ([prompt guide](https://ai.google.dev/gemini-api/docs/lyria-prompt-guide)). It filters some prompts, including some that name artists, and it cannot change tempo or key without a hard reset. The project uses Gemini for all AI work ([requirements AI-1 to AI-7](../requirements.md#interpretation)).

## Requirements

1. Turn any request into valid Lyria input, or refuse it.
2. Support relative requests against the current program.
3. Validate output before it reaches Lyria.
4. Make at most one model call per request, within free-tier limits.
5. Keep Discord identifiers out of model input.

## Options considered

1. **Send the request text to Lyria unchanged.** No extra call or latency. However, requests that name artists are often filtered, tempo and key cannot be set, requests that are not about music cannot be refused, programs have no titles, and relative requests are impossible.
2. **One Gemini call with structured output.** A JSON schema generated from Pydantic models constrains the response, and Pydantic validates it. One call handles translation, relative requests, refusals, and titles. `gemini-3.5-flash-lite` is a stable model on the free tier and supports [structured output](https://ai.google.dev/gemini-api/docs/structured-output).
3. **A multi-turn agent with function calling.** More flexible, but it needs several calls per request and adds latency without benefiting a single mapping from text to settings.

## Decision

Option 2, with option 1 as the fallback. The interpreter returns `Interpretation(kind, plan)` as specified in the [architecture](../architecture.md#interpreter). The model defaults to `gemini-3.5-flash-lite` and can be changed with `SOBAFM_GEMINI_MODEL`. Calls time out after 10 seconds. Not-music requests and safety blocks are refused; errors, timeouts, and invalid output fall back to the request text as a single prompt. Lyria's generation settings are fixed in code rather than chosen by the model.

## Consequences

- Each request costs one extra round trip to Gemini before music changes; the median latency target is 15 seconds from `/play` to audio (NFR-1).
- Request text is sent to Google, and Google may review free-tier prompts; the README says so (USE-1).
- Interpretation quality needs an evaluation set, run on demand with an API key (AI-7).
- Model retirements require changing the default model in a release; operators can override it in the meantime.

## Revisit triggers

- The default model is deprecated or leaves the free tier.
- Evaluation results fall below their targets.
- Lyria RealTime gains native handling of free-form requests.
