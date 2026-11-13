# Checked Jamendo stream ownership

This implements slice C of [#198](https://github.com/slackysoba/sobafm/issues/198)
and the [adopted engineering contract](https://github.com/slackysoba/sobafm/issues/188#issuecomment-6795394426).
It consumes the merged metadata/admission, HTTP transport and journal. It does
not itself enable policy, call a model, lease capacity or authorize a source
request. The station integration wires this handle under explicit operator
configuration; see [operations](operations.md#jamendo-discovery-and-checked-streaming). Current-main/source/Discord qualification remains
pending. The refused live route remains inconclusive for stream access.

## Interfaces for station integration

| Interface | Contract |
| --- | --- |
| `RefreshCandidate(transport: StationTransport, policy: Policy)` | One by-ID request through the station's metadata gate, then the existing `refresh` evaluation. Returns a fresh `Locator` or `Refusal`; uses the successful fetch's completion timestamp, never its cleanup timestamp. No retry or cached admission. |
| `JamendoStream(candidate, bank, *, refresh_candidate, journal, open_audio, command=None, clock=time.monotonic)` | One immutable ranked Candidate and one bank already leased by this station. Inject the shared refresh callable, Journal and `scope.open_audio`. Construction holds readiness but performs no external work. The optional command is for the existing Python-child tests; production uses unchanged stock mpg123. |
| `await handle.prepare() -> Prepared \| None` | Starts the single owned run and waits for screened readiness or failure. A successful frozen `Prepared` contains the **refreshed** Candidate, complete `RenderedCredits` and journal attempt number. It never returns ranked credits as refreshed approval. Cancelling the wait stops and joins the owned run. |
| `await handle.run() -> Completion` | Starts or joins that same run through actual I/O settlement. Frozen Completion separates transfer, envelope, decoder report, PCM facts, observed encoded hash/count, failure, transport report, cleanup clock/verdict, I/O settlement and final release. It does not mean the whole recording was heard. |
| `handle.cancel()` / `handle.report()` | Nonblocking stop and immutable current facts. Cancellation discards emission immediately; repeated calls preserve the earliest applicable stop and all retained owners. |
| `await handle.release() -> bool` | After `run` settles and the caller successfully releases the bank, durably record settlement and release the journal pin. Returns false before those prerequisites. A storage failure keeps the pin and requires journal restoration before retry. |
| `MediaProducer.stream(candidate, bank)` | Dispatch to the injected `stream_factory`, returning this owned handle. Only a Jamendo Candidate is accepted. Existing `produce(recording, bank)` retains the exact operator-manifest file size/SHA-256 path. No source URL is invented from a Recording. |

`AudioInput` is the concrete `OwnedAudio` surface expressed as a protocol so
ordinary tests can independently pause read/close settlement. Production passes
the merged `scope.open_audio`; there is no provider registry or alternative
production transport. `VerifiedStreamInput` is a separate decoder input type:
bounded checked handoff, encoded/PCM bounds and screening/completion callbacks.
It cannot impersonate a local file's known size or hash.

Integration D must reserve station and bank capacity before constructing a run;
keep at most one prepared next stream; and retain only metadata until the heard
recording has at most 30 seconds estimated remaining, or skip/early drain needs
the replacement. No repeated open extends a prefetched locator. D offers the
exact `Prepared.candidate.recording` and `Prepared.credits` to the source switch,
updates heard attribution only on that switch, and marks every failed/tried key
in the existing bounded program history. A failed preparation does not own or
cancel another current source. There are no Station mutations in this module.

On retirement D calls cancel, awaits run despite repeated cancellation, releases
the bank only after I/O settlement, and calls release. A held bank view retains
both the bank and journal pin. Heard credit-menu snapshots remain immutable and
independent; neither a menu nor a settled journal entry authorizes another play.
An early `prepare()` failure is not settlement: the caller still joins `run()`
and performs this retirement. A successful run may finish I/O while its bank
still contains valid audio awaiting the consumer's natural drain.

## Validation and readiness

Refresh uses `Refreshed.candidate` even when `previous_decision_matches` is false.
The journal persists the complete refresh-purpose decision and credits before
the opener consumes its one-use locator and pinned attempt. Refresh failures,
changed content, removal, disabled policy, journal capacity/write failure and
stale locators open no audio. Unresolved/quarantined keys and uncertain usage
retain the shared journal's existing refusal behavior.

The HTTP opener requires status 200, exactly one Content-Length, accepted content
type and identity encoding. It refuses chunked/close-delimited audio and trailers
under the [adopted framing refinement](https://github.com/slackysoba/sobafm/issues/190#issuecomment-6796182251).
Header limits have the separately [adopted normalized-field meaning](https://github.com/slackysoba/sobafm/issues/190#issuecomment-6798626947).
No response is silently converted to a file, alternate route or fresh attempt.

For integer duration D in 1–86400 seconds, let
`h = min(3000, max(200, D), 10*D)` hundredths of a second. This is the accepted
`T = min(30, max(2, .01*D), .10*D)` expressed exactly. PCM is bounded by
`ceil((100*D+h)/2) * 3840` produced bytes. Final duration compares integer stereo
samples against `h*48000/100`, so one extra sample cannot disappear through
floating-point rounding. The transport owns the corresponding duration-derived
encoded cap and `2*(D+T)+60` wall deadline; the stream retains its own wall timer
through downstream decode after clean HTTP EOF. Readiness has the original
15-second audio-open budget. Refresh has its separate ten-second transport bound.

`Envelope` walks complete Layer III frames, with standard MPEG 1/2/2.5 bitrate
and sample-rate tables. VBR and padding are accepted; free/reserved bitrate,
reserved rate/version/emphasis, other layers, junk, incomplete frames and
version/rate/channel-count changes are refused. It never scans compressed frame
payload for tag signatures or resynchronizes. Original checked bytes go to
stock mpg123 with its existing `--no-resync`, environment and limits. SHA-256
identifies observed encoded bytes only; it is not a known-good source hash.

Leading encoded tags total at most 131072 bytes, and later tags/trailers have an
independent cumulative 131072-byte bound. ID3v2.3/v2.4 revision zero is supported,
with no extended header, unsynchronisation, experimental/compressed/encrypted
frames; a validated v2.4 footer is supported. Fixed ID3v1 is accepted only at the
final boundary. APE and unsupported structures are refused. There are at most
256 ID3 frames, at most 8192 bytes per text/notice frame, and at most 32768 UTF-8
bytes of decoded admission text. Known APIC JPEG/PNG image payload can be
discarded after bounded MIME/type/description validation; it stays inside the
encoded tag cap and is never decoded as an image.
The supported field and footer layouts follow the primary
[ID3v2.3 specification](https://id3.org/id3v2.3.0) and
[ID3v2.4 structure specification](https://id3.org/id3v2.4.0-structure);
footer and padding cannot coexist. The smaller admission limits are this
project's adopted contract, not limits claimed by those specifications.

Text/URL, comment, terms, lyrics and people fields use strict encodings and
terminators. Unknown IDs and unsupported structured variants such as SYLT fail
closed. Supplied text must reconcile with the refreshed immutable title,
creators, backlink, exact license, notices or retained descriptions; title and
creator fields additionally match their respective identities. A new required
text field is refused rather than guessed to be harmless or appended to credits.
This deliberately conservative subset may reject ordinary provider files whose
embedded text is absent from the refreshed evidence. Such compatibility needs
separately authorized evidence, not silent dropped fields. Existing notice
conflict checks remain, with an additional exact license-path comparison that
preserves jurisdiction. Empty text and equivalent duplicate notices add no credit.

The existing decoder reader accumulates one 3840-byte PCM frame, screens it, then
commits it to the bank. Peak <=16 is silence; the 1501st consecutive silent frame
fails. At least 19 of 1920 samples at >=32767 or <=-32768 makes a clipped frame;
the 51st consecutive clipped frame fails. These are structural defect checks,
not a listening-quality verdict. The same reader and feeder threads are used;
there is no extra checker thread, process or native slot.

A held fresh bank cannot become ready at its automatic threshold. For D>=6,
reconciled leading tags and committed/rendered refreshed credits release the
hold, and six seconds of screened PCM establishes readiness. For D<6 the hold
remains until all completion checks succeed. At the end, require exact HTTP
completion, complete MP3 envelope, successful uncancelled decoder exit, valid
PCM, duration and defect checks. Existing final-PCM semantics stay intact:
stereo-sample alignment and at least one complete frame are required; a shorter
final remainder is neither padded nor emitted. Audible drain is a separate fact.

## Buffering and ownership

The [shared transport accounting](jamendo-catalog.md#receive-copies-and-counters)
remains 983040/1048576 network bytes and 503040/524288 read/write bytes per slot,
inside ADR 0012's unchanged 294842368 controlled-byte total. The network reader
retains one <=16384-byte chunk. The async/feeder bridge owns one fixed 16384-byte
handoff, and the existing feeder owns its 16384-byte write buffer. The envelope
uses one fixed 131072-byte tag backing, also reused for a single MP3 remainder
(whose standard frame length is below 16384), never a second full tag join.
Frame/tag output views alias that backing and are consumed before reuse. Text,
normalization and canonical UTF-8 temporaries remain in the adopted subcaps.
The PCM reader owns one partial/screen frame and scans samples without a sample
array allocation. No copy borrows another station or a voice/drain partition.

Full banks block the existing reader; the single handoff then blocks the feeder
and network pump. The transport's five-second input gap runs only while actually
requesting bytes. Readiness and absolute wall deadlines continue through
backpressure. This is bounded resident streaming, not whole-track buffering.

Clean HTTP EOF starts immediate cleanup of its own response/DNS/quota owner.
It does **not** terminate valid downstream decoding into a backpressured bank.
This [recorded consumer composition](https://github.com/slackysoba/sobafm/issues/198#issuecomment-6799183923)
preserves the distinct stage facts in ADR 0013. Decoder output EOF retains its
existing graceful exit check. An actual cancellation/failure adopts the earliest
bank, failed HTTP or decoder timestamp, sends TERM immediately, and retains the
existing +2-second KILL and +3-second failed verdict. HTTP cleanup failure or
expiry also stops/discards the stream, retaining its original timestamp.

Late framing, integrity, duration, severe defect or notice failure immediately
closes emission and discards unread bank PCM. Already handed-off frames cannot
be recalled and credits are never rewritten. Notice conflict recording starts
before waiting for blocked decoder/HTTP close; write failure preserves the
journal admission latch and unresolved initial attempt. Cancelled reads,
writes, DNS, journal jobs, child/process groups and threads remain owned through
actual settlement. Failed cleanup stays failed after late release; a blocked
bank release retains its pin and debt. There is no automatic restart.

Decoder output counts include rejected frames/bytes as observed output, separately
from bank-admitted PCM. A delayed terminal journal write remains on the decoder's
original I/O cleanup deadline; expiry stops emission and its permanently failed
verdict is written after the original job actually settles. Normal audible drain
does not keep that completed I/O cleanup timer running.

## Verification boundary

Tests use synthetic tags/MP3 envelopes/PCM, the existing Python children, real
loopback HTTP transport and deterministic paused ownership fixtures. They cover
fragmentation, short-track gating, exact sample/cap boundaries, notice changes,
quarantine, silence/clipping schedules, full-bank HTTP EOF and cancellation.
They do not execute mpg123, acquire music, contact a provider or Discord, build
an image, establish native containment/useful fit, or qualify release readiness.

Shared transport corrections were independently reviewed and merged in
[#206](https://github.com/slackysoba/sobafm/pull/206), closing
[#205](https://github.com/slackysoba/sobafm/issues/205). The real transport
integration cases exercise that boundary; synthetic owner tests separately
cover deterministic delayed-settlement schedules.
