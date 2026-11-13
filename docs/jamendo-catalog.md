# Jamendo metadata and admission boundary

The offline boundary in `jamendo_catalog.py` and `jamendo_admission.py` implements
the first slice of [ADR 0013](decisions/0013-jamendo-discovery-streaming.md).
It makes no network request, reads no configuration or credentials, persists
nothing and opens no audio. Runtime playback still uses the 167 admitted local
FMA recordings. The saved-source importer and FMA manual reviews are unchanged.

## Interfaces and ownership

| Interface | Owner and contract |
| --- | --- |
| `Policy(enabled=False)` | Trusted operator configuration only. Neither model output nor provider JSON is a configuration source. The fixed local version is `jamendo-stream-1`. |
| `Query(search, artist_name, name, tags)` | The following search/ranking slice constructs it **after semantic musical-intent validation**. This boundary validates field sizes, nonempty query and exclusion of URLs/markup; it does not decide whether text is musical. |
| `SearchWindow(query, policy, offset=0)` | One discovery attempt. `request()` returns a credential-free `Request` or typed `Refusal`. `receive(exact_request, response_bytes_or_refusal, observed_at)` consumes that request once. Only one may be outstanding; at most two pages of 100 are issued. |
| `SearchResult` | Immutable candidates, coverage, refusals, issued-page/received-byte counts and next offset. Candidates have a `Recording`, a ranking-only `Decision` and a material-content digest, never a stream address. |
| `parse_listing(row, expected_id=None)` | Complete untrusted row to an immutable, transient `Listing`, or a safe `AdmissionInputError.reason`. This is not the older importer, which drops unknown fields and therefore cannot provide this policy's complete evidence. |
| `evaluate(listing, policy, observed_at, purpose)` | A new immutable `Decision` binding key, canonical metadata digest, all normalized policy-evidence digest, version, reason, UTC refresh time and ranking/refresh purpose. `UNKNOWN` recording kind remains unknown: admission is explicitly provisional. |
| `refresh_request(key, policy)` / `refresh(request, bytes_or_refusal, ranked, policy, observed_at)` | Construct a by-ID request, then evaluate its new response. Result is `Refreshed` or `Refusal`; failed refresh never falls back to search metadata. This must run immediately before **every** audio open, including cache hits. |
| `Refreshed` | A new candidate/credit snapshot, an evidence-comparison flag and a transient stream URL excluded from its representation. It is not audio-integrity/readiness approval and is not a reusable playback permit. Only its candidate is suitable for a future bounded provenance journal; never serialize the wrapper or `Listing`. |

The separate transport below uses the fixed HTTPS tracks endpoint, validates the
request again, and injects the client ID separately. An issued pure `Request` is
not evidence that a network call happened. Actual calls, bytes, quota and retained
operation ownership belong to transport. This pure metadata boundary starts no
task or cleanup clock; returned bytes still require `receive` or `refresh`.
Do not construct a fresh window to evade the attempt's two-page limit.

The search/ranking owner must wait until the window settles before ranking its
candidates: a conflicting duplicate on page two revokes the page-one candidate.
Repeated identical IDs are deduplicated; inconsistent and malformed duplicates
are quarantined throughout the window. Coverage is always partial, empty-window,
unavailable or disabled, never whole-catalog exhaustion. Windows contain at most
200 returned rows, inside the 256-record/source envelope. Offset is page-aligned
and below 153,600 (768 cycles times 200 positions); program cycle/history limits,
the three-attempt backoff and 512,000-character pooled matching ceiling belong to
the following discovery slice. No retry occurs here.

## Validation and evidence

Requests explicitly include singles and album tracks and ask for `mp32`, music
information and license flags. The [Jamendo tracks reference](https://developer.jamendo.com/v3.0/tracks)
documents these fields; they are not full-recording or complete-credit guarantees.
Each response is at most 1 MiB of UTF-8 JSON, with duplicate keys, nonfinite literal
numbers, excessive nesting and inconsistent result counts rejected. API errors and
nonempty warnings fail the page without copying their potentially sensitive text.

Identity, primary creator, title, source backlink, positive whole-second duration
(within the existing 86,400-second catalog bound), exact license and ordinary
stream route are required. Optional album fields may be absent or empty for singles.
There is no 1–15-minute filter. Missing/unknown credit structures and unsupported
fields fail closed; provider `approved`, policy or review fields cannot grant
admission. Known previews, excerpts, snippets, stems, loops and sample packs reject
in title, type, descriptions, details, tags and notices. Explicit warning lists,
rights-conflict wording and conflicting source/license links reject. Text-only
license claims need reconciliation to an exact URI rather than guessed equivalence.
Notice URL validation and conflict screening share token extraction. Scheme/host
case and an explicit HTTPS port do not bypass screening or change a matching
notice's meaning; license paths and recording IDs remain exact, and credits retain
the original supplied spelling.
This conservative text screening can refuse legitimate titles; it does not prove
the absence of undisclosed conflicts or recognize every possible natural-language
rights statement.

The finite license set follows the [Creative Commons license list](https://creativecommons.org/licenses/):
BY, BY-SA, BY-ND, BY-NC, BY-NC-SA and BY-NC-ND at generic 2.0/2.5,
unported 3.0 and international 4.0; generic 1.0 uses BY-ND-NC rather than
BY-NC-ND. Supported ports are US 3.0 and France 2.0. The exact supplied version and
jurisdiction remain in the URL and label. Other ports, versions, public-domain
instruments and unknown families are refused, not silently converted. The known
public HTTP Jamendo/CC backlinks are upgraded to HTTPS without changing their
path. No declaration here changes the accepted permission or certifies rights.

Every supplied creator and notice remains intact. Descriptions and explicit detail
text also enter credits so a credit-bearing sentence cannot be dropped as a hint.
Credits use the existing immutable `CreditSnapshot` and full safe renderer. Bounds
are 16 creators of 200 characters and 16 notices of 1,024 characters including
provider/composition notices; reject rather than truncate. Matching details keep
the existing 24 by 160-character limit. Unsupported markup, controls and links
with credentials, queries or fragments are refused in retained text.

The evidence digest covers every normalized identity, credit, descriptive and
license-flag input, and the validated ordinary-route category. Changes to tags or
other `Recording.details` invalidate it even when the older `metadata_digest`
stays the same. Known transport-only media/image links, waveform/statistics,
position and the download flag do not authorize admission and are not retained
or hashed. The ordinary route is currently the documented HTTPS
`prod-N.storage.jamendo.com/` track-ID query form with `format=mp32`; unfamiliar
routes fail closed. Credentials/ephemeral query values and the URL itself never
enter a candidate, decision, digest or error. This is request-shape validation,
not DNS, redirect, access or actual-stream qualification.

Refresh re-evaluates policy and both bindings. Changed valid notices or licenses
produce new credits and a new decision, with `previous_decision_matches=False`.
Identity or descriptive/content changes refuse the old ranked choice for bounded
continuation; the earlier immutable snapshot is never rewritten. Policy-version
changes similarly cannot inherit approval. `decision_matches` only compares
evidence; it cannot establish freshness or authorize cached audio. Leading and
later embedded notices are the stream producer's responsibility, including
discarding buffered audio on late conflict and preserving heard credits/debt.

## HTTP transport and ownership

`jamendo_transport.py` and `jamendo_journal.py` implement prerequisite A of the
[adopted engineering contract](https://github.com/slackysoba/sobafm/issues/188#issuecomment-6795394426).
They are not imported by runtime configuration, the station, adapter or media
producer. No discovery loop, decoder, MP3/tag checker or PCM producer is added.

The [adopted framing refinement](https://github.com/slackysoba/sobafm/issues/190#issuecomment-6796182251)
uses public aiohttp HTTP/1.0 and explicit `Connection: close` controls. Metadata
accepts a complete bounded Content-Length or connection-EOF document. Audio
requires exactly one Content-Length. Both reject Transfer-Encoding and Trailer
headers before application consumption: the locked client otherwise accepts
unannounced chunk trailers without exposing them. HTTP success alone grants no
source admission, and valid JSON alone is not a successful Jamendo envelope.

| Interface | Ownership and caller obligations |
| --- | --- |
| `JamendoTransport(client_id: str, journal: Journal, *, quota: Quota = UNCONFIGURED_QUOTA, clock=time.monotonic)` | One process-wide client, separate API credential, caller-owned journal. No configuration/env-file read. Closing marks it closed before joining owners. |
| `client.reserve_station() -> StationTransport` | At most three live scopes. Reserve only after the corresponding existing station reservation, before interpretation. The fourth scope fails before any request. A scope is retained through cleanup. |
| `await scope.fetch(request: Request, *, deadline: float \| None = None) -> bytes \| Refusal` | Revalidates the existing credential-free request. One active operation plus one waiter per scope; retained cleanup holds the active gate. Absolute ten-second budget includes the wait. For a two-page window, the search owner supplies the **same loop-monotonic start + 20 s deadline** to both calls. |
| `scope.last_report`, `scope.failed_cleanups` | Safe last-operation category, dispatch/response counts, raw body bytes or explicit unknown, numeric HTTP status, response completion and original stop/settlement times. Failed cleanup count survives a subsequent report. Do not log an aiohttp exception or request. |
| `Locator(refreshed: Refreshed, completed_at: float)` | Ephemeral non-repr wrapper. Use the successful refresh fetch's `last_report.completed_at`, not the later cleanup time. The locator consumes itself on its first attempted open and expires after five seconds; its candidate must have an admitted refresh decision with current UTC time. |
| `await scope.open_audio(locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float \| None = None) -> OwnedAudio` | Requires the exact durable, pinned, unused attempt and an already leased uncancelled bank. At most two HTTP audio owners per station. Consume the journal attempt once; constructing another locator cannot reuse it. Caller retains the bank/decoder ownership independently. |
| `StreamLimits.for_duration(duration: int) -> StreamLimits` | Validates 1–86400 integer seconds; derives `T=min(30,max(2,.01*D),.1*D)`, encoded cap `ceil(41000*(D+T))+262144` and absolute wall budget `2*(D+T)+60`. The encoded cap uses exact integer hundredths (`h=min(3000,max(200,D),10*D)`, cap `410*(100*D+h)+262144`), avoiding an extra byte from floating-point rounding. These are counters/deadlines, not allocations or traffic entitlements. |
| `await owner.read() -> bytes` | Exactly one reader, at most 16384 bytes per return. Five-second gap timer runs only while requesting bytes. Empty bytes mean verified HTTP length completion only, not MP3/decoder/PCM completion. Caller must keep only its one bounded handoff. |
| `owner.ready_deadline`, `owner.stopped`, `owner.report` | The following stream producer must enforce the unchanged 15-second readiness deadline and watch the stopped event to discard PCM on failure. The HTTP wall-deadline timer continues during backpressure. Clean HTTP EOF closes its own owner with `transfer_complete=True` and `report.kind=None`; it does not cancel viable downstream decoding. Actual failure/cancellation/expired cleanup propagates the earliest applicable `report.stopped_at` into stream cleanup. |
| `owner.cancel(first_stop: float \| None = None)` / `await owner.close()` | Stop reads, abort transport without a drain, join a pending read, real connection-close notification, DNS and journal reconciliation. Repeat cancellation cannot reset the first stop or free capacity early. A close after the original three seconds remains expired after settlement. |
| `scope.cancel(first_stop: float \| None = None)` / `await scope.close()` / `await client.close()` | Stop every affected operation and audio owner before joining any cleanup, including across all stations on client shutdown. Each owner retains the earliest stop clock. Never reopen a closed client. Close the shared journal only after transport and stream owners finish. |

Metadata uses 3 s connect/pool/DNS, 5 s response-header/read-gap and 10 s absolute
operation bounds. Audio has 3 s connect/DNS, 5 s response-header, demand-based
5 s reads, the caller's readiness deadline and its duration-derived wall deadline.
Both request identity encoding, disable decompression, use no cookies/proxy/netrc
or environment credentials, verify TLS normally, and follow no redirects. A
fresh public middleware guard refuses a second handler dispatch; force-close and
HTTP/1.0 alone demonstrably retry GET after a disconnect.

The resolver validates every answer, including mixed results, and rejects
non-global and IPv4-mapped IPv6 addresses. Only the fixed API host or exact
numeric storage-host grammar reaches it. Checked addresses go directly to the
connector, with caching disabled. Cancelled DNS work is joined before release.
Audio retains only the refreshed ordinary mp32 route and optional opaque locator
values; it receives no API client ID/header from the metadata transport.

Automatic EOF cleanup joins an already completed read without cancelling it.
Pending reads still cancel and join; explicit cancellation remains a failure even
if it arrives after EOF. Repeated close/cancel retains the earliest stop and an
expired cleanup verdict after late settlement. An unavailable raw-body counter
(including aiohttp's empty reader on a refused response) remains unknown. Cleanup
still settles the HTTP owner and records known dispatches while retaining the
full uncertain quota reservation through restart; it never infers zero usage.

Before constructing a JSON tree, metadata validates strict UTF-8, depth <=8,
<=32768 lexical tokens and <=65536 decoded UTF-8 bytes per string, including
surrogate-pair accounting. The syntax pass rejects duplicate keys and nonfinite
numbers. The existing metadata/admission code then owns success-envelope,
identity, field and evidence checks. Raw pages and locators never enter the
journal. Request/body exceptions expose only fixed safe categories.

### Receive copies and counters

The implementation targets the existing CPython 3.14 Linux selector loop and
locked aiohttp 3.14.5, not arbitrary event-loop replacements. Public
`socket_factory` supplies a socket whose `recv` and `recv_into` explicitly cap
delivery at 32768 bytes; SO_RCVBUF is also set to 32768. The supported
`request_class` hook sets TLS read high water to 32768 **before sending HTTP**,
captures the exact connection owner, and refuses TLS without that control.
The aiohttp low/high water marks are 16384/32768. Read-ahead can overshoot high
water within one delivery; `read(16384)` is not the queue bound.

The adopted slot totals remain unchanged. Additional library copies are charged
inside those slots, with these conservative subdivisions for the later stream:

| Network slot payload | Bytes |
| --- | ---: |
| Received body queue including delivery overshoot | 98304 |
| Header wire/text copies, status and field temporaries | 409600 |
| Retained tags, decoded notices, normalization temporary | 3 × 131072 |
| Canonical notice UTF-8 | 32768 |
| Application read, handoff, MP3 remainder | 3 × 16384 |
| **Total / existing network slot** | **983040 / 1048576** |

The parser is configured for 32 fields, 1020 bytes per component and 4081 bytes
for a reason (leaving the HTTP status-line prefix/CRLF). The compiled parser can
accept a name and value each up to 1020 bytes: this setting alone does not bound
their sum. Before body consumption or status acceptance, explicit raw-header
validation requires `len(name) + len(value) + 4 <= 1024`, including the canonical
colon/space/CRLF projection after the maintained parser removes surrounding
optional whitespace. This is the [explicitly adopted field-content refinement](https://github.com/slackysoba/sobafm/issues/190#issuecomment-6798626947),
which supersedes the earlier complete-wire-field interpretation. Discarded leading
whitespace has **no configured aggregate wire-byte cap**: it is processed
incrementally within the unchanged 5-second response-header deadline and
10-second metadata absolute deadline, with bounded retained payload and current
delivery copies. The Python parser's whole-line interpretation is stricter. No
socket-prefix limit, parser replacement or global runtime switch is adopted.

Pre-validation copies are charged even for a rejected field. The 409600-byte
header subdivision covers 33 fields (including the count-overflow field) with
two 1020-byte raw components, an ASCII name and a worst-case four-byte-per-character
decoded value: `33 * 7 * 1020 = 235620` bytes. Add `5 * 4096 = 20480` for raw and
decoded status and `2 * (65536 + 1024) = 133120` for oversized-field/error
concatenation temporaries, totaling 389220 bytes. The Python parser's retained
whole lines plus raw/decoded fields fit within that larger compiled-parser bound.
The delivery itself is already included in the read/write subdivision below.
Header objects and allocator overhead remain in the whole-service group; these
payload copies are charged separately. No slot or whole-service cap increased.

Fragmented HTTP/TLS regressions deliver 4 MiB of leading spaces or tabs in observed
16384-byte fragments. GC-visible retained header bytes/text do not grow between
the 16 KiB, 1 MiB and 4 MiB checkpoints, and only the small JSON body enters the
body counter. The 4 MiB fixture is not a maximum or proof of native-heap/RSS fit.
Unfinished-header regressions exercise the actual header timeout, abort, repeated
cancellation and restart with the complete unknown-usage reservation preserved.

| Read/write slot payload | Bytes |
| --- | ---: |
| CPython's fixed SSL receive backing | 262144 |
| TLS incoming staging, including a bounded receive overshoot | 65536 |
| Decrypted delivery pieces and their simultaneous join | 2 × 65536 |
| Decoder feeder and simultaneous write copy | 2 × 16384 |
| Screen frame, sample scratch, partial frame | 3 × 3840 |
| **Total / existing read/write slot** | **503040 / 524288** |

The socket receives into the fixed SSL backing, so that view is not charged as
another allocation. CPython's SSL copied-read path can retain pieces alongside
their joined delivery; both are included. Setting TLS high water does not shrink
its fixed 262144-byte backing. Offline real-client TLS tests exercise delivery,
paused body queues and cancellation without consumption. These bounds still need
independent review and actual-image useful-fit qualification; they do not claim
total RSS, kernel reclamation, native decode or six-stream runtime qualification.

Before dispatch, the journal reserves one call and the operation's full encoded
cap **plus 98304 read-ahead bytes** against configured aggregate limits. This is
conservative, especially for long tracks, and is not a new allowance. At actual
settlement, `total_raw_bytes` counts received body bytes, including refused and
unconsumed buffered bodies; metadata/audio totals remain separate. Counts describe
application bodies, not encrypted/TLS/wire billing. Valid Retry-After values on
denial/429 establish a not-before time; absence of a usable value latches that
client until replacement. The transport never sleeps/retries a provider request.

Discarded header whitespace is not body usage. These counters do not establish
total network billing or a provider/host traffic allowance. Any deployment limit
defined on another metric requires separate qualification and enforcement before
claiming it is honored; this refinement grants no traffic or spending entitlement.

If the maintained parser fails **before exposing a response**, no public raw-body
counter exists. The safe report uses `body_bytes=None`, persists the known
dispatch/failure count and retains the entire byte reservation. An interrupted
write can also leave an uncertain reservation. Neither error, cancellation nor
restart refunds it. Settled body totals are known counts, not invented exact totals
for those failures. [The counter limitation](https://github.com/slackysoba/sobafm/issues/190#issuecomment-6796927635)
is explicit; conservative unknown usage can exhaust configured capacity sooner.

## Bounded provenance journal

`await Journal.open(path, *, clock, writer=atomic_write)` loads one exclusive
instance on the existing writable data volume. `clock` and `writer` are injected
for deterministic tests, not provider extensions. Invalid, duplicate, noncanonical
or oversized state refuses startup. An interrupted `.tmp` snapshot requires
explicit recovery; it is never silently removed or promoted. There are no backups,
WAL, raw-response archives or audio files.

| Journal API | Durable meaning |
| --- | --- |
| `await reserve_attempt(candidate: Candidate) -> Attempt` | Validate a safe **refresh-purpose** candidate and complete credits, persist an unsettled entry, then pin it. Search decisions cannot create attempts. An unsettled/restarted or quarantined key cannot be reserved again. Maximum six pins. |
| `claim_attempt(attempt)` | Consume a current pinned initial entry once, immediately before audio opening. It cannot be reused by a second locator or after a failure. |
| `await commit(number, facts: Facts, *, settled: bool, conflict_digest=None)` | Update transfer/decode/PCM, observed encoded hash/bytes, decoded samples, failure and cleanup facts. Never replace the entry's decision or credits. An expired cleanup stays expired; a recorded failure cannot become success. A semantic conflict binds a specific digest and reason. |
| `release_pin(number)` | Only after recorded actual settlement. An expired cleanup can settle late while preserving the failed verdict. |
| `await credit_used(number)` | Extend normal retention from actual last credit use without granting approval or changing the snapshot. |
| `blocked(key)` / `snapshot` | Inspect immutable safe provenance and quota state, not reusable playback approvals. |
| `await resolve(number, *, expected_evidence_digest, expected_conflict_digest, operator_review)` | A trusted operator action resolving exactly that retained evidence/conflict, only after runtime pins end. An unrelated metadata change never resolves it automatically. No operator command is wired by this slice. |
| `await reserve_usage(channel, body_bytes, quota) -> Reservation` | Persist configured call/byte reservation before external work. At most nine unresolved reservations: three metadata plus six audio. |
| `await settle_usage(reservation, *, dispatches, responses, body_bytes, failed)` | Reconcile known actual usage only for a live reservation after underlying owners settle. |
| `await retain_unknown(reservation, *, dispatches)` | Persist known calls/failure while retaining all uncertain body budget. Restart cannot claim that budget is free. |
| `await restore()` / `await close()` | Retry the pending conservative snapshot after a storage failure, or mark closed and join the writer. Restoring never silently clears quarantine or unknown usage. |

The canonical UTF-8 snapshot has <=256 entries, <=131072 bytes per entry, and
<=4194304 bytes total, including a <=4096-byte counters/reservations envelope.
Current plus temporary file is <=8388608 payload bytes. One executor writer and
at most six waiters serialize mutations. Writes flush, fsync, replace, fsync the
directory, then publish the new in-memory snapshot. Repeated cancellation joins
the same job; it cannot abandon a write or publish early. A failed late-conflict
write latches new Jamendo admission and retains its pending snapshot for durable
restoration. Initial persistence failure permits no audio.

Persisted entries contain the exact safe Recording/Decision/content-digest
projection, its integrity checksum, separate completion/cleanup facts, and UTC
settlement/use/expiry times. The existing catalog validator and credit renderer
validate the projection. Metadata digest is checked again; the extra checksum
detects changes to retained details too. The policy-evidence/content digests cannot
be reconstructed from this intentionally smaller projection and are validated as
bindings, not turned into a source-response archive or a new approval claim.

Settled unpinned entries expire 24 hours after settlement/last credit use. Startup
prunes them; a single timer drives later expiry through the same writer. Earliest
settled entries may be evicted sooner for either cap. Pins, unresolved restart
attempts and semantic/rights conflicts never disappear through TTL/LRU. Expiry
write failure is explicit and latches admission. When protected entries fill a
cap, reject new Jamendo work while leaving FMA/unrelated audio to its own owner.

Tests use temporary journals, synthetic loopback HTTP/TLS, delayed owned DNS/write
work and the existing synthetic certificate fixture. They cover byte/structural
caps, malicious routes/addresses, framing, dropped GET dispatches, buffered-byte
accounting, cancellation, exact Unicode credits, quota uncertainty, quarantine,
entry capacity and atomic-write faults. They do not establish source compatibility,
music quality, decoder behavior, Linux whole-service fit or release readiness.

Search/ranking and checked streaming may consume these interfaces only after
review and merge. Their semantic intent, two-page deadline, MP3/notices/screening,
readiness, whole-stream completion and earliest bank/HTTP/decoder cleanup owner
remain necessary. Station/configuration/Discord integration follows those slices.
No live/source request or runtime enablement follows from these offline modules.
