# 0012: Reserve capacity for three stations under one bot identity

Status: **Implementation proposal; numeric contract awaiting maintainer acceptance before merge.**
Issue: [#172](https://github.com/slackysoba/sobafm/issues/172).
Route choice: [#166](https://github.com/slackysoba/sobafm/issues/166#issuecomment-6791659984),
where the maintainer chose **“One bot identity; implement three-server capacity.”**

## Problem and options

The former two global banks let one station prepare its next track, but two
stations could consume both and leave each other without replacement capacity.
Three stations were unsupported. Increasing the container limit alone neither
adds bank/copy slots nor prevents one guild from consuming another's slots.

The selected route is one process and one Discord identity with three station
reservations. The rejected alternative was three separate bot identities and
isolated services. No sharding, replica coordination or provider framework is added.

## Proposed explicit contract

This proposal supersedes only the numeric limits and shared two-slot admission
parts of ADRs [0001](0001-linux-resource-proposal.md),
[0004](0004-amd64-whole-service-adoption.md),
[0008](0008-lyria-realtime-path.md) and
[0011](0011-program-refill.md). Those accepted records remain historical records.
The former below-100,000,000 controlled-byte and at-most-34,560,000 PCM guarantees
do **not** describe this candidate. The implementation is prepared under the
route choice; adopting these replacement guarantees is the remaining concrete
maintainer decision. Recommendation: accept these exact bounds for subsequent
qualification, with no runtime-fit claim. Alternatively retain the old bounds
and keep this implementation unmerged; that cannot provide the selected capacity.

| Reservation | Candidate bytes | Ownership |
| --- | ---: | --- |
| Docker-owned whole-service group | 268,435,456 | Full group charged to every playing server |
| Six PCM banks | 13,824,000 | Three stations × two banks × 2,304,000 |
| Six network-message slots | 6,291,456 | Two per station × 1,048,576 |
| Six read/write slots | 3,145,728 | Two per station × 524,288 |
| Three PCM scratch slots | 1,572,864 | One per station × 524,288 |
| Three cancellation/assembly slots | 1,572,864 | One per station × 524,288 |
| **Copies subtotal** | **12,582,912** | All simultaneous payload copies |
| **Controlled total** | **294,842,368** | Group + banks + copies, never divided by three |

The conditional aggregate PCM bound is
`6 × 12,524,288 + 13,824,000 + 12,582,912 = 101,552,640` bytes.
Each native decoder retains its reviewed AS/DATA/STACK/CORE limits; at most six
PCM-bearing native spaces, including descendants and retained cleanup debt, may
occupy the six slots. Exact native closure, launch and backing premises still
require qualification. Each slot can serve a catalog producer or generated
session, never both simultaneously. Zero swap, 64 tasks and two CPUs remain;
their useful three-station fit is unmeasured too.

The full group and all global reservations count toward **every** playing server.
No baseline subtraction or residual allowance is introduced. The shared OOM
failure domain remains: exhausting the group can stop all stations. This is not
total RSS or a host-RAM recommendation. Allocator/object overhead, native voice
encoding/encryption/packet backing, kernel pipe/socket storage, narrow decoder
kernel PTY backing and uncharged shared/mapped backing remain the named exclusions.
No new host-handler exclusion is added. Physical/kernel backlog remains unbounded
by this contract; no three-second kernel reclamation result is claimed.

## Admission and lifetime

`PcmBankPool` owns three explicit `StationReservation` objects, keyed by guild.
Each reserves two producer/bank/copy slots before `/play` or `/freestyle` invokes
interpretation. A fourth station receives a capacity reply before any model,
provider, decoder, bank allocation or voice playback starts. Pending requests
count even before they own a bank. Merely joining an idle voice channel reserves
no station; voice connection overhead still lives inside the whole-service cap.

Catalog prefetch, generated renewal and mixed-mode replacement use only the
guild's two slots. They cannot borrow another guild's idle slot, so the old
cross-guild silent-program priority is removed. Failed preparation preserves
playable current audio. Readiness stays six seconds, and retries and capacity
waits keep their finite existing bounds.

Each station also has two voice-reader credits, each retaining up to two
handed-off frames. The existing conservative 217,344-byte scratch accounting
fits inside each station's 524,288-byte scratch reservation. A replacement voice
reader cannot consume another station's scratch. Exhausted credit returns shared
silence without consuming PCM. Credits follow returned bytes through disconnect.

A stop or disconnect does not return the station reservation while any request,
native producer, generated session, bank view or voice credit is retained. A
same-guild restart reuses that reservation, including its debt; it cannot gain
another pair. Actual release makes the station slot available to another guild.
Stale references cannot allocate from a returned reservation. Repeated cancellation
does not reset the original three-second cleanup clock. Failed verdicts remain
failed after late release; debt remains charged until actual settlement.

## Evidence and limits

Deterministic offline tests establish admission, reservation isolation and
controlled cancellation schedules only. Historical Q1–Q4, L2, decoder and Discord
results retain their original source/image/target bindings. In particular, the
[corrected L2 run](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6789773813)
was five synthetic pairs/ten sessions in 27 seconds on application `01e2226` and
image `bd7d1aee`, with harness `37e2b8f`; its earlier failed attempt remains.
The retained five-pair harness still exercises one two-slot reservation and its
historical limit, not three stations or this candidate's fit.

Separately authorized qualification must cover simultaneous three-station catalog
boundaries, generated renewals, mixed modes, retained debt, stop/restart, useful
memory/task/CPU fit and original-clock release on the actual candidate image and
host. Existing 60-minute continuity and latency criteria remain. No live Lyria
access or six-session provider concurrency is established. Broad catalog access
is a separate deployment prerequisite in [#171](https://github.com/slackysoba/sobafm/issues/171).

This proposal authorizes no native/media/Docker/provider/Discord activity, source
acquisition, spend or host changes. [#151](https://github.com/slackysoba/sobafm/issues/151)
remains deferred. Native arm64 stays on hold under
[#60](https://github.com/slackysoba/sobafm/issues/60), with its CI job and preflight
tooling unchanged. No historical evidence qualifies this numeric candidate.
