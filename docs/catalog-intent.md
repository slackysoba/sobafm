# Initial catalog intent

`gemini_match.interpret_catalog_intent(request: str, *, client: GeminiClient) -> Query | Refused`
implements the independent initial interpretation step accepted in
[ADR 0013](decisions/0013-jamendo-discovery-streaming.md) and
[#191](https://github.com/slackysoba/sobafm/issues/191). It is not wired into `/play` yet.
Existing prepared-catalog matching and ranking are unchanged.

The helper checks the listener request before making exactly one call through the
injected shared client. Empty, over-200-character or control-containing input raises
`CatalogInputError` with no call. Surrounding whitespace is stripped after checking
the original length. There is no retry or raw-request fallback. The caller retains
client ownership and closes it; the helper never creates or closes a client.

The body is built only inside the client's admitted callback. It contains the
JSON-encoded request and a fixed instruction/schema, with no catalog metadata,
credits, Discord identity fields, audio or credentials. Musical text itself is not
necessarily anonymous. The existing Interactions API, `gemini-3.5-flash-lite`,
`store=false`, shared four-call gate, ten-second budget including gate wait,
64,000-byte transport response limit and 2,048 output-token limit remain unchanged.
The [provider disclosure](provider-access.md) still applies.

`parse_catalog_intent(text: str) -> Query | Refused` independently checks the whole
model answer; schema hints are not a validation boundary:

- Exactly `{"outcome":"search","query":{"search":"…","artist_name":"…","name":"…","tags":[]}}`
  returns an immutable `jamendo_catalog.Query` only after `Query.parameters()` passes.
  Search, artist and title are strings of at most 200 characters, with an empty
  string for an unused field. There are at most eight tags of at most 64 characters.
  At least one field or tag is nonempty. Existing whitespace, control, URL, markup
  and tag-plus-separator restrictions apply without repairing or truncating values.
- Exactly `{"outcome":"refuse","query":null}` returns `Refused`.
- Duplicate, extra or missing fields, wrong types/outcomes, invalid query content
  and malformed JSON raise `MatchFailed` (`output_shape` or `output_not_json`).
  No provider text is included in the error message or logged by the helper.

The instruction interprets musical imagery generously, retains legitimate artist
and title lookup, and refuses plainly unrelated requests or instruction injection.
It does not apply generated-music artist-imitation restrictions. Local validation
establishes the allowed structure; offline tests do not prove the model's semantic
judgment or live API schema compatibility.

Provider failures and blocks propagate the existing safe `MatchFailed` kind and
`blocked` fact. A provider block is not converted to `Refused`. Timeout produces no
query; cancellation propagates after the shared client's pending operation settles.
The helper changes no station state, so callers must still reject stale results.

A returned query grants neither recording admission nor permission to contact a
source. Source requests, search windows, pooled ranking, journals, program history,
configuration and station integration remain later work. That orchestration must
reuse the validated initial intent on refill, rather than reinterpret it.

`tests/test_catalog_intent.py` uses scripted transports to exercise strict parsing,
query boundaries, request validation, one-call/privacy behavior, shared client
ownership, gate/deadline behavior and cancellation. Existing matcher/transport
tests retain coverage of ranking, response-byte limits and real loopback HTTP.
No live provider, music-source or Discord evidence is claimed.
