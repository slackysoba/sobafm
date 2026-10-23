# Buffered input candidate

Issue [#236](https://github.com/slackysoba/sobafm/issues/236) prepares the next
derived SDK version, **2.26.0+sobafm.2**. Its additive `buffered-input.patch` is
applied after the unchanged, reviewed sobafm.1 patch. `BUFFERED-INPUT.toml` pins
both the base and candidate source hashes. `PUBLISHED-INPUTS.toml` binds the
original materials to the retained published sobafm.1 manifest. Those original
materials, release assets and application dependency remain unchanged.

## Public input path

`BoundedReceiveOptions.input_storage` optionally accepts one distinct, exclusively
owned 4,096-byte bytearray. The music connector passes a one-claim factory through
WebSockets' public `create_connection` argument. The connection extends the normal
ClientConnection with asyncio.BufferedProtocol; it reuses the ordinary parser,
message/fragment limits and pause/resume behavior. Omitted input storage keeps
the previous path. The legacy/default receive and compression options are intact.

The supplied buffer bounds each standard buffered plaintext callback before
WebSockets creates Frames. `buffer_updated(n)` passes only an immutable copy of
its used prefix, at most 4,096 bytes. The caller prepays that possible copy
separately from the fixed backing. A copied-read callback refuses before parsing;
it never becomes a fallback which accepts a larger TLS join. Factory reuse,
including after failed construction, refuses. The caller must not share the
backing with another options/connection owner before actual context quiescence.

A large frame or message spans callbacks without creating additional WebSocket
fragments. At most 1,369 nonempty audio Frame owners can be involved in a batch,
including the four conservative boundary owners. Counting all carriers instead
gives 2,052 owners and at most 4,099 event/deque reference entries before capacity
rounding. References alias their Frames; separately retained bytearray payloads
and shared container backings remain charged. The existing later 64-fragment
guard is not represented as this earlier materialization bound.

## Owned build and verification

Use the already-retained authenticated upstream archive, signed Python 3.14,
existing uv/Git and a fresh short output directory:

```powershell
& ./third_party/google-genai/build_buffered.ps1 -Archive $sdkArchive `
    -Output $candidateOutput -Python $signedPython
```

The additive recipe reuses the original source extraction and tree inventory
helpers, preserves its pinned setuptools build route, and verifies both patches,
the complete path allowlist, pre-notice candidate hashes, notice-only prefixes,
licenses and wheel/sdist/source correspondence. It refuses existing outputs and
preserves failures. The matching sdist contains original and additive materials.
The recorded artifact hashes identify one build; reproducible wheel bytes are
not assumed.

Install the wheel only into a fresh owned verification environment using the
unchanged exported runtime requirements with google-genai omitted, then run:

```powershell
& $checkPython -I -B ./third_party/google-genai/check_buffered_candidate.py
```

The checker reuses the original compatibility cases, with the new identity
checked separately. It exercises actual installed SDK modules and the ordinary
WebSockets parser, all used-prefix lengths, fixed/fallback/factory guards, large
frames split across batches, and tiny nonempty fragments before collector
failure. A fake SSL read object exercises the standard buffered dispatcher;
no SSL context/engine, socket, provider, default-auth Client or native API is used.

## Activation and ownership

Application backing owners prepay two exclusive slots of
`524288 + 4096 + 4096 = 532480` bytes each: message payload, fixed input storage
and immutable forwarding-copy capacity. Reservation precedes all allocations.
Existing setup/iterator/context and sticky three-second cleanup ownership covers
the pair. Duplicate allocation/owner claims refuse; failures and uncertain closure
retain their capacity. Neither retirement, queue pop, error, GC nor a cleanup
deadline refunds it. Every remaining model/transport/player obligation stays
unknown and fail-closed.

**The application still installs sobafm.1.** New SDK argument forwarding and exact
dependency activation await coordinating-root clearance of the immutable new
publication, in this same issue/branch/PR. No release/tag/publication occurs from
this recipe, and no unverified URL is added to application metadata. Candidate
checks do not qualify physical allocator/kernel/native/player backing, a complete
70 MB envelope, a production profile, catalog readiness or #130 completion.
