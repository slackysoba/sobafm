# Engineering notes

These constraints guide implementation and testing. They describe required engineering behavior, not completed release qualification.

## Delivery and dependency scope

Prove one eligible recording end to end in the intended Linux container before expanding the playback architecture. Validate resource enforcement in the same non-root, default-seccomp deployment. A cgroup design must demonstrate usable delegation, namespace reachability and migration permission.

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

Bound untrusted input, response sizes, queues, concurrency and retries independently of process limits. Qualify useful workloads and actual release behavior in the intended Linux container. If a requirement prevents a viable design, present one measured alternative and its tradeoff.

## Catalog quality and rights

Evaluate enjoyment, request fit and recording fidelity separately. Curate eligible recordings before playback and retain source identity, provenance, attribution and retrieval/storage policy. A playable URL, an API flag or a consumer subscription does not establish the required playback permission.

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
