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
with its historical limits and shared OOM failure domain. The accepted three-station
contract in [ADR 0012](decisions/0012-three-station-capacity.md)
sets explicit new limits and admission. Historical proof does not qualify
the application: the actual image, entrypoint, native dependencies, target kernel,
page size and core-routing premises still need qualification at their real
boundaries. Native arm64 is on hold until after the first amd64 release
([#60](https://github.com/slackysoba/sobafm/issues/60)).
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
stays charged, with the first-cancellation deadline unchanged. ADR 0012
adopts three station pairs and their copy partitions; no historical
evidence is transferred to the larger configuration.
Require actual userspace/process/FD settlement before a subsequent pair; a
closed descriptor, EOF or exited leader alone cannot establish that settlement
or prove prior kernel objects were reclaimed. Preserve failed verdicts and
retained userspace debt. No three-second kernel-release guarantee, eventual
reclamation result or physical/kernel backlog bound is established. Exact-image
native sanitization, complete payload accounting and useful-fit qualification
remain separate requirements; the exclusion is not execution permission.

Bound untrusted input, response sizes, queues, concurrency and retries independently of process limits. Qualify useful workloads and actual release behavior in the intended Linux container. If a requirement prevents a viable design, present one measured alternative and its tradeoff.

## Catalog quality and rights

Pool admitted source candidates and [rank them with validated Gemini calls](decisions/0006-gemini-play-matching.md), [eight recordings at a time](decisions/0011-program-refill.md). The implemented path uses prepared FMA; [ADR 0013](decisions/0013-jamendo-discovery-streaming.md) accepts bounded Jamendo search and provisional checked streaming, not yet implemented. Deterministic regressions must cover output rejection, provider failures, a stop during matching, source-specific admission, immutable credits and honest coverage. Listening is optional deferred evidence; metadata match never establishes enjoyment or fidelity. Retain source identity, provenance, attribution and retrieval/storage policy. A playable URL, API flag, local policy decision or consumer subscription does not establish complete playback permission.

For Jamendo, reject invalid musical intent before any source request. Bind every admission input, including relevant details and notices, into the policy-evidence digest alongside canonical metadata; refresh by ID before each audio open. Exercise changed policy/credits/details, a failed refresh, late notices and integrity failures before and after readiness. A late failure discards that stream's buffered audio and retains its owner/debt on the original clock; it cannot rewrite heard credits or silently restart the recording. Preserve FMA's manual reviews and local size/hash checks. Search/page/response, history, cycle and no-progress limits are the finite bounds in ADR 0013, not permission for provider activity or evidence of whole-catalog exhaustion.

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

The [#44 evaluation](https://github.com/slackysoba/sobafm/tree/81a7519/evaluations/fma_validation) applied these stages to the supplied Night Owl candidate, keeping technical decode, source correspondence, embedded notice review and catalog admission separate. Its one approved real run passed. The evaluation, its regression guidance and its tests were removed from `main` in [#104](https://github.com/slackysoba/sobafm/issues/104); commit `81a7519` keeps them.
