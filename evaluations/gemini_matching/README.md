# Gemini matching evaluation

`try_requests.py` shows what Gemini ranks for real requests, so match quality can be judged before a listening session. It plays no audio and doesn't touch Discord. It isn't part of `tests/` or CI.

Check the prompt size without calling anything:

```sh
uv run python evaluations/gemini_matching/try_requests.py --dry-run "a haunted carnival on the moon"
```

Send requests to Gemini over 20 invented songs, using a key set in your own shell:

```sh
uv run python evaluations/gemini_matching/try_requests.py --send-to-gemini \
    "a haunted carnival on the moon" "something like Daft Punk" "what is the capital of France?"
```

Add `--catalog` and `--reviews` to use a prepared snapshot instead of the invented songs.

**Authorization.** Each request is one billable call, at most 20 per run. Run it only with the maintainer's go-ahead.

**What is sent.** The request text and each song's title, artist, length, labels and details, along with the fixed instruction in `gemini_match.py`. Requests are sent with `store` off. Google's free tier may still use content to improve its products; the paid tier doesn't, according to its [pricing page](https://ai.google.dev/gemini-api/docs/pricing).

**What a run establishes.** The request shape, response parsing and how sensible the ranking looks for those requests against that catalog. It doesn't establish enjoyment, audio quality or latency on the deployed service. Record the date, model and catalog with any result you keep, and keep the poor matches too.

## `/freestyle` plans

`try_freestyle.py` shows what plan Gemini returns for real `/freestyle` requests, so interpretation can be judged before any music is generated. It generates no audio and doesn't touch Discord. It hasn't been run: it needs a maintainer-supplied key and go-ahead.

Check the request sizes without calling anything:

```sh
uv run python evaluations/gemini_matching/try_freestyle.py --dry-run
```

Send its twelve cases to Gemini, or your own requests instead, using a key set in your own shell:

```sh
uv run python evaluations/gemini_matching/try_freestyle.py --send-to-gemini
uv run python evaluations/gemini_matching/try_freestyle.py --send-to-gemini "a haunted carnival on the moon"
```

The cases cover plain and absurd requests, each control, an artist's style, a request for lyrics, a relative request after a previous plan, and text that should be refused. A case is marked `PROBLEM` when it falls back, gets the wrong outcome or names the artist it asked about, and the run then exits 1. Judge the plans themselves by reading them.

**Authorization.** Each request is one billable call, at most 20 per run. Run it only with the maintainer's go-ahead.

**What is sent.** The request text, the previous plan for the one relative case, and the fixed instruction and schema in `freestyle_plan.py`, with `store` off. The free-tier note above applies here too.

**What a run establishes.** That the live API accepts the schema, that the response parses, and how sensible the plans, refusals and artist translations look for those requests. If the API rejects the schema as too complex (`400 INVALID_ARGUMENT`, reported here as `http_400`), Gemini's [structured output guide](https://aistudio.google.com/docs/structured-output) suggests dropping numeric bounds first; local validation keeps them regardless. A run doesn't establish that Lyria sounds like the plan, that wordless vocals stay wordless, or anything about latency on the deployed service. Record the date and model with any result you keep, and keep the poor plans too.
