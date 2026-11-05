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
