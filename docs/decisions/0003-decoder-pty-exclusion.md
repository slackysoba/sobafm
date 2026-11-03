# 0003: Exclude narrow decoder kernel PTY backing

Status: **Accepted resource-contract amendment** (2026-11-02, America/Los_Angeles).
Issue: [#44](https://github.com/slackysoba/sobafm/issues/44).
Proposal: [reviewed options](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6588607792).
Acceptance: [maintainer decision](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6593814582).

This record changes only the decoder kernel PTY exclusion and subsequent-pair
admission semantics. It preserves [ADR 0001](0001-linux-resource-proposal.md) as
the historical group-accounting decision and leaves its caps and experimental
scope unchanged. It does not adopt the application architecture proposed in #7.

## Problem and evidence

The stock reviewed decoder's diagnostic sanitization requires its terminal path.
The reviewed proposal records that PTY kernel backing is not all covered by
`memory.current`, and that teardown can continue asynchronously. Descriptor
close, EOF and helper/container exit do not establish identity-specific kernel
reclamation. Existing evidence therefore does not prove complete PTY backing,
a three-second kernel-release bound or eventual reclamation.

The controlled-capacity contract already excludes kernel pipe/socket storage.
Whether to extend that exclusion to this decoder's kernel PTY backing is a
resource-contract decision, distinct from useful-fit or execution approval.
The proposed 524,288-byte runner / 262,144-byte PTY subdivision was neither proved
nor accepted; no observed overflow is asserted by that missing proof.

## Options

1. **Retain the previous contract.** Keep the maintained binary and hold PTY
   qualification for complete backing/reclamation evidence. A maintained,
   upstream-supported pipe sanitizer/control could be investigated, but none is
   established by this decision.
2. **Exclude only decoder kernel PTY backing alongside kernel pipe/socket
   storage.** Preserve the stock decoder and all userspace reservations. Permit
   a subsequent pair after actual userspace/process/FD settlement, without
   claiming that excluded earlier kernel objects have been reclaimed. Physical
   or kernel backlog can remain unquantified.
3. **Propose a private native sanitizer patch.** This could remove the PTY
   dependency but changes the approved distribution-binary supply and requires
   maintenance, security, licensing, source/ELF/image and native-target review.
   No patch or native-build exception is approved here.

## Decision and acceptance

The maintainer answered **“Approve the narrow PTY exclusion (recommended)”** on
2026-11-02 in America/Los_Angeles. The linked acceptance records **option 2 only**.
This preserves the controlled-capacity promise while making its physical-memory
tradeoff explicit; it does not claim a total-memory guarantee.

Only the decoder's **kernel PTY backing** joins the existing kernel pipe/socket
exclusions. Userspace audio, tags, diagnostics, selected evidence, application
buffers, serialization and IPC copies retain their existing ownership and
charges. Passing payload through a PTY does not reclassify its userspace copies
as excluded kernel backing. The full group cap is still reserved; there is no
baseline subtraction or new pool.

The unchanged experiment is:

```text
G = 90,177,536
P =  4,608,000
C =  4,194,304
G + P + C = 98,979,840
```

Every original userspace partition remains unchanged. For #44, those copy
partitions are intake owner 786,432; metadata helper 2,097,152; decoder runner
786,432; and outer controller 524,288 bytes. The proposed 524,288/262,144
runner/PTY subdivision is not adopted. Other-owner and complete runner-copy
accounting still require evidence within these reservations.

A subsequent pair may be admitted **only after actual userspace, process and
file-descriptor settlement**, with all other admission and qualification gates
satisfied. The original first-cancellation clock and three-second userspace
cleanup obligation remain. Repeated cancellation cannot renew them; timeout
cannot erase a failed verdict, free a still-owned reservation or abandon debt.
Held operations, payload references and uncertain closes remain owned until
their actual settlement. This decision does not create automatic slot reuse.

Settlement for that admission rule is not a claim that prior excluded kernel
objects were reclaimed. There is no three-second kernel-release guarantee, no
proved eventual reclamation result, and no bound on the physical/kernel backlog
left by repeated sessions. Those limitations must remain visible in operator
and developer documentation.

## Remaining qualification and execution boundaries

The source/security gates and stock reviewed decoder remain in force. Bind
qualification to the actual committed source, image, native target, decoder/ELF
and interpreter/init identities. Review the exact implementation against this
narrow exclusion and its userspace accounting/settlement rules; complete runner
and other-owner payload overlap, non-root/default-seccomp containment and useful
topology fit still need evidence. Unchanged historical results qualify only
their original source/image/target cases.

Before owned-audio qualification, separately review and release the bounded
no-media PTY preflight and required volume/slot preflight. Native diagnostic
sanitization must pass the fixed tag, encoding, ordering and cancellation cases
under the actual image. Before supplied-recording intake, require the applicable
native parser/decoder, original-clock cleanup, full-stream and source-bound
qualification evidence. Offline tests do not replace those checks.

This acceptance does not authorize native, media, volume, Docker, provider or
Discord execution, source acquisition, a custom native supply, or host-security
changes. It does not approve #7's application architecture. The supplied Night
Owl expiry remains **2026-11-09T00:00:00Z**; neither this decision nor a later
qualification can renew it. Preserve failed results and retained debt alongside
later evidence.
