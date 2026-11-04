# Engineering notes

These constraints guide implementation and testing. They describe required engineering behavior, not completed release qualification.

## Delivery and dependency scope

Prove one eligible recording end to end in the intended Linux container before expanding the playback architecture. Validate resource enforcement in the same non-root, default-seccomp deployment. A cgroup design must demonstrate usable delegation, namespace reachability and migration permission.

The [accepted experimental interpretation for #7](decisions/0001-linux-resource-proposal.md) permits the ordinary Docker runtime to own the entire service's capped group and place processes before input, without delegating cgroup management to the application. Q1-Q4 proved that runtime authority, visible effective limits and descendant membership on native amd64; retain the full shared cap in every server's calculation.

The [recorded initial native-amd64 proof](linux-feasibility.md#recorded-native-amd64-decoder-proof)
supplies bounded owned-fixture useful-fit, aggregate PCM and four controlled
process-release results, with distinct case source/image bindings. On that
evidence, the [accepted amd64 adoption](decisions/0004-amd64-whole-service-adoption.md)
permits building the first eligible-recording playback slice around this group,
with every limit and the shared OOM failure domain unchanged. It does not qualify
the application: the actual image, entrypoint, native dependencies, target kernel
and page size still need qualification at their real boundaries. Native arm64 is
on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60)).
ADR 0001 remains unchanged.

Keep the initial release path to one repository, one image build, one Compose example, one data volume and one operator guide. Add infrastructure only for demonstrated requirements. Prefer maintained decoder distributions and SDKs over custom supply chains. If a dependency needs a patch, retain a small reproducer, supported version, rationale and exit condition.

## Audio boundaries

Centralize stereo signed 16-bit PCM at 48 kHz: 192,000 bytes per second and 3,840 bytes per 20 ms Discord frame. Producers prepare PCM away from the event loop and voice callback. The callback must not block on network, process I/O, database work or cleanup.

Distinguish transfer completion, decoder completion, valid PCM and audible drain. A decoder exit, short read or low buffer does not alone prove a complete recording. Treat inaccurate duration metadata as an input to a documented completion policy, not an infallible oracle.

Desired program, producer state, ready audio and heard source are separate facts. Now-playing state and credits follow the connected player's actual source switch. A prepared, cancelled or failed replacement must not overwrite information for music still audible.

## Ownership and cancellation regressions

Use deterministic tests that can pause delivery, completion and cancellation independently. Cover these schedules:

- Cancelling an await does not end the underlying read, write, HTTP close or native operation. Retain the owner and join the original operation before replacing it.
- A message may be delivered before its sender completes. A valid subsequent request must not be rejected solely because terminal write completion remains owned.
- Once a terminal response is committed, cancellation must not emit a second contradictory terminal response.
- Cancellation can overtake a permit/grant handshake. Settle committed grants without entering cancelled provider work, weakening correlation, accumulating an unbounded retired-request set or killing unrelated channels.
- A dispatcher must not wait for work that requires another message from the same dispatcher.
- Cleanup retains one owner and the original deadline. Repeated cancellation must not reset it. A timeout is a failed verdict, not proof that memory, process slots or obligations were released.
- Failed replacement preparation preserves already admitted playable audio where possible.

Exercise real parent-reader and dispatch boundaries where applicable. Do not use sleeps to make race tests pass.

## Capacity and isolation

Use the exact controlled-capacity definition and exclusions in [requirements](requirements.md). Do not describe it as total RSS or a complete security sandbox. Account for retained workers, prepared replacements, copies and cleanup debt before new admission.

The [accepted decoder-PTY exclusion](decisions/0003-decoder-pty-exclusion.md)
covers kernel backing only. Userspace diagnostic/audio/application/IPC payload
stays charged, with the original partitions and first-cancellation deadline.
Require actual userspace/process/FD settlement before a subsequent pair; a
closed descriptor, EOF or exited leader alone cannot establish that settlement
or prove prior kernel objects were reclaimed. Preserve failed verdicts and
retained userspace debt. No three-second kernel-release guarantee, eventual
reclamation result or physical/kernel backlog bound is established. Exact-image
native sanitization, complete payload accounting and useful-fit qualification
remain separate requirements; the exclusion is not execution permission.

Bound untrusted input, response sizes, queues, concurrency and retries independently of process limits. Qualify useful workloads and actual release behavior in the intended Linux container. If a requirement prevents a viable design, present one measured alternative and its tradeoff.

## Catalog quality and rights

Implement the [accepted three-source selector](decisions/0002-prompt-catalog-selection.md) with deterministic regressions for fit ordering, complete recordings, hard constraints, immutable credits and missing/partial coverage. Listening is optional deferred evidence; metadata match never establishes enjoyment or fidelity. Curate eligible recordings before playback and retain source identity, provenance, attribution and retrieval/storage policy. A playable URL, an API flag or a consumer subscription does not establish the required playback permission.

Use source-specific acquisition. Complete-file validation or deleting audio after use does not expand rights. Separate technical defects from taste; intentional lo-fi texture is not automatically a defect. Test improved encodings for availability and reliability, not only nominal fidelity.

Make complete source-bound credits available to ordinary listeners. Test private credit pagination, expiry and missing optional status permissions. Apply catalog transition constraints from the requirements rather than assuming generated-audio fades are suitable for recordings.

## Evidence

Build evidence in stages:

1. Unit/component invariants and deterministic failure schedules.
2. Linux container integration for processes, permissions, decoder behavior and shutdown.
3. An authorized Discord test with an eligible source.
4. Endurance, latency, replacements, skips, reconnects and failure recovery.
5. A clean-machine walkthrough using the actual release image.

A pass at one stage does not replace another. Bind evidence to the actual commit, image and native target. Retain failures and limitations alongside successes and reuse valid unchanged checks.

The [#44 evaluation](../evaluations/fma_validation/README.md) makes those boundaries
explicit for the supplied Night Owl candidate. Its source-only packet separates
technical decode, source correspondence, embedded notice review and catalog
admission. New init/owner/helper topology and terminal diagnostics require fresh
native fit evidence before source intake; synthetic tests and unchanged native
ELF hashes cannot substitute for it. Mutagen remains a pinned evaluation-only
dependency with preserved GPL notices and explicit pre-parser allocation limits.

Environment/counter regressions use synthetic fragmented streams and controlled
read/close schedules. Cover the cumulative transaction budget as well as each
record limit, conflicting/escaped/duplicate mount destinations, CPU overflow,
counter key-set changes/decreases and terminal delta overflow. Final resource
observation must finish before any clean-settlement claim. A held or uncertain
final close retains its descriptor and staged facts; a late cleanup observation
cannot reset the first cancellation clock or turn unknown timing into success.
Do not retry a failed final snapshot or hide sender/receiver resource payload
inside raw control scratch. These offline checks do not qualify native behavior.

Keep diagnostic source bytes in their fixed charged backing. Range scans must
preserve six complete labeled tag rows, optional final newline and exact raw/tag
hashes without copying a suffix, split values or joined block. The format regex
only receives its bounded 34-byte alternative; long completion-minute digits
must not become an unbounded integer/string conversion. Test capture allocation
after fork, exact-cap sentinel reads, held read/hash/write ownership and cached
facts retained after report rejection. Producer/FD settlement alone does not
refund diagnostic backing or immutable report aliases.

Canonical reports preflight exact size, then recheck bounded exact builtin
values while filling an indexed arena. Stable caller ownership is required;
same-size concurrent mutation is not a synchronization contract. Compare bytes
against stdlib JSON across Unicode key order, controls, surrogate cases, numeric
spellings and newline capacity. Keep scalar/sort scopes out of immutable freeze
and verify rejected arenas release with GC disabled and retained tracebacks.
Clear completed parser/build exception frames and context links before fallback.
Once a success record may have been submitted, including an uncertain first
write at cursor zero, metadata cannot append a rejection terminal. Retain the
immutable record through partial/EINTR/held/error writes and journal fsync/close;
deadline expiry or failed close never renews the clock or releases live debt.
The bounded construction component does not establish receiver `J(shape, wire)`
for parsed values, rejected inputs or widening Unicode. Keep that gap explicit
within the unchanged intake/controller partitions.

Receiver tests must exercise predecode rejection, not only validation after
`json.loads`: wide escaped values and keys, unknown/deep/duplicate structures,
scalar domains and actual producer failure/null variants. Preserve Latin-1
notice variants, Unicode packet-bound paths and noncanonical meaningful JSON
numbers; long finite float tokens have their own bounded conversion transient.
Cache completed records once while checking every suffix and aggregate stream
cap. Hash original received bytes. Distinguish metadata final EOF from terminal
LF, and keep the earliest authenticated cleanup envelope when detail fails.
Verify interrupted helper retirement cannot publish history twice. Exercise
setup/configuration graph retirement, held attach plus cleanup, completed
exception payloads with GC disabled and retained journal worker/record debt.
Neither selected projection size nor source-path aliases close decoder `J` or
the complete retained-fact ledger; compact intake phases retain separate charges.

Keep compact intake and evaluation reports on one production-owned encoder with
distinct caller policies. Verify historical v1/v2 fingerprint vectors and stdlib
wire parity, Unicode/surrogates, accepted string data and integers above 128 bits.
Hash through fixed borrowed chunks in canonical order without a fingerprint-size
cap. Size-only gates still own scalar spellings; reject impossible sized integers
before conversion and retain maintained conversion failures. Distinguish state
work-plus-scalar 3L from work-plus-result 2L, with source graphs and concurrent
operations separate. Construction must precede temp removal/open. Exercise actual
partial/EINTR/invalid writes and held write/fsync/close/replace/parent-fsync under
the original clock. Test arena/view release with retained exceptions and disabled
GC; translate encoding failures into the cleanup boundary. Bind the shared module
in the packet. Ordinary CLI formatting, submission and flush ownership remain a
separate output obligation; these compact checks do not qualify that path.

Exercise fixed receive capacity separately from used bytes, including empty
responses, dirty unfilled tails, fragmented prefix overflow and retained views.
Opaque stderr hashes only its accepted prefix and retains no body. Block real
pipe read/hash/close operations across cancellation; verify no capacity or owner
release and no new prefix after hash failure. Late/failing allocation and an
interrupted thread start must preserve registered owners and original deadlines.
Retirement under a borrowed view cannot drop backing or publish another receipt.
Count immutable read results until their explicit end, and distinguish the
removed explicit bytes conversion from unproved codec/JSON internal copies.
Test rejected wide cleanup names and valid Unicode setup families separately;
the old rejected-input subtotal is not a surviving family maximum or fit proof.

Its failure regressions include nonzero helper exit after full delivery, early
write exit with immediately available or later-drained terminal evidence,
simultaneous helper failure, absent/partial/malformed/cross-case records, repeated
cancellation and held descriptors. Terminal timing adoption precedes successful
result validation and output retirement. Valid failed receipts can tighten the
original clock; invalid receipts make timing unknown, and neither can certify
resource release. The separate owned-synthetic qualification verdict never turns
an expected pipeline failure into `passed` or substitutes for native fit evidence.
