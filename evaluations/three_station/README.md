# Three-station candidate probe

**Prepared protocol; no actual-image result yet.** This is the finite harness phase of
[#212](https://github.com/slackysoba/sobafm/issues/212). Merge does not close that issue.
After independent review of this exact harness and invocation, the coordinator assigns
one execution and independent result review. A failed attempt stays failed; diagnose a
demonstrated setup defect before proposing a retry. No new release gate is introduced.

## Binding and scope

The only candidate is source `fed87387b7ea8ab9eff2d516ad9d425d18b50f71`, image
`sha256:ae44556ff603f47e52d000cb7cc7f9b33ed289d0e3b176da582fbf16c99d35cc`.
Reuse [accepted packaging evidence](https://github.com/slackysoba/sobafm/issues/210#issuecomment-6844677283),
including the installed source, decoder/dependency inventory and target premises. Do not
rebuild, install packages in the image, or transfer historical runtime qualification.
The runner verifies the local image ID, source revision label and Linux/amd64 identity.
It compares its files to the supplied reviewed harness commit before starting anything.
The probe uses the image's installed package with isolated Python, applies the production
crash controls before native imports, and calls the installed startup limit gate.

ADR 0012 applies unchanged: group 268,435,456 bytes; six banks 13,824,000;
copies 12,582,912; **controlled total 294,842,368 per playing server**. Zero swap,
64 tasks, two CPUs, and six native slots including debt. Every stock decoder uses the
production AS/DATA/STACK/CORE limits. No allowance is divided by three or subtracted.

The concrete fixture is the existing admitted local FMA recording **9423, “Hash God”
by Welcome Wizard**, album *Ob/Gyn*, listed duration 112 seconds. It is 1,793,243 bytes,
SHA-256 `ab0cf8ce5dbb75c072bda2cf9f8ae6cda261cdf7da505eee1e825011a00f5159`,
with the existing manifest's 21,699,840-byte finite PCM output ceiling. It was selected,
validated and accepted under [ADR 0007](../../docs/decisions/0007-fma-dataset-batch-catalog.md)
and [#122's final acceptance](https://github.com/slackysoba/sobafm/issues/122#issuecomment-6661615097).
Provider: Free Music Archive; [source recording](https://freemusicarchive.org/music/Welcome_Wizard/ObGyn/Hash_God);
[Attribution 3.0 United States](https://creativecommons.org/licenses/by/3.0/us/).
The copy and listing come from the FMA research dataset (Defferrard et al., 2017), whose
metadata is CC BY 4.0. The stored file's license notice agrees with the admitted credits.

Preparation verified the existing file's size/hash and used the application's `eligible`
function against the current local catalog/reviews. Its accepted metadata digest is
`cb818dc6747032d514e1d73cfc7a9f537915c96f56f4bb76eb3e25393fc8e3ff`, with
`credits_complete=true`, `access_ready=true`, `synthetic=false`. Before execution confirm
that this admission remains present; the runner checks the adjacent existing review and
media records as well as the exact audio identity. The target verifies size/hash again
before decoder input. Mount only this file read-only; never copy or mount the full catalog.
Cancellation intentionally precedes full-file EOF. This qualification consumes PCM into
a discard sink and does not broadcast, fade or mix the catalog recording. If the exact
file/admission is absent, stop as not-run; no substitute, acquisition or regeneration.

## Finite matrix and reached production paths

All cases run in order in one fresh container; a failed case prevents the next.
All three reservations must exist before work, refuse the fourth owner, retain two
exclusive slots apiece and return only after actual settlement.

| Case | Workload and observation | Required result |
| --- | --- | --- |
| Catalog | Three `PcmBankPool` reservations, two real `DecodeProducer`/mpg123 children each, all six banks full and all six leaders live by pidfd. Read 200 frames per station at 20 ms intervals, switching successively from first to second prepared bank halfway. | Six concurrent native slots, 600 whole frames, no borrowed slot; record nonzero-frame count without assuming the recording has no opening silence. Repeated stop preserves each first cleanup deadline; actual producer and bank release within it. |
| Generated | Three real `GeneratedStream` instances, real aiohttp WebSockets to owned loopback PCM. Expedite only the 540-second renewal trigger, wait for all three overlapping pairs, then consume each 200-frame crossfade at 20 ms intervals. | Six session/bank slots; 600 mixed frames with the exact 1000-to-2000 linear sample progression, incoming sample 2000 after handoff; exactly six sessions and three crossfades; no failed cleanup/debt. |
| Mixed | Two catalog pairs plus one generated renewal concurrently. | Four live decoders plus two generated slots; 600 frames; same successive catalog switch, exact generated handoff and clean release. This is mixed simultaneous station load, not an adapter mode-switch test. |
| Retained debt | Three reservations, one real decoder; hold its real input file's close on an event. Start the ordinary finisher immediately, cancel twice, and wait for the unchanged three-second deadline. Then allow the same close to settle. | The deliberately expired cleanup stays `expired`; producer, bank and reservation remain charged, same-guild reserve returns the same owner, fourth is refused. After actual late release the failed verdict stays failed and a new owner can reserve. |

The peer serves eight sessions total, two per fixed route, at most eight two-second
messages each: 24,576,000 PCM bytes (32,768,000 base64 characters plus bounded JSON/control
framing) over loopback, with no file output. Additional session attempts are refused and
make the run fail; production retry limits remain unchanged. No more than eight sessions
receive payload. Each control message is limited to 4,096 bytes; every handler has a
20-second deadline. The peer and its payloads deliberately live **inside** the measured
group; their overhead is not subtracted. This plain WebSocket peer reuses the L2 message,
sample and renewal mechanisms; it does not measure TLS backing or live Lyria concurrency.

Maximum successful workload: 11 decoder starts over all cases (six simultaneous), eight
generated sessions (six simultaneous), three load windows of about four seconds, one
deliberate three-second expiry. Readiness waits are bounded at 15 seconds; the inner
work deadline is 90 seconds and the host operation deadline is 120 seconds. Finishing
an owned operation may outlast an inner timeout; the host then stops only this named
container and reports failure. No second workload starts while release is outstanding.

## Reviewed invocation, once assigned

Use the existing Linux host tools and check environment; do not install another one.
Before execution, recheck the agreed storage limits: 10 GB new project bytes, stop heavy
work at 8 GB or under 25 GB free, logs/exports at most 500 MB. This run adds less than
1 MB of receipts/logs, no image/volume/environment and no fixture copy. Its one Docker
log is capped at 32 KiB; the host accepts at most 32 KiB/16 probe records. Retain only the
sanitized JSONL and a private execution receipt with exact invocation/exit status.
No credentials, real environment files, raw URLs or Docker Env output are needed.

From the clean reviewed checkout, replace the two placeholders with the reviewed full
harness SHA and the existing exact fixture's absolute path (not a directory). This is
the complete operation, using the existing Python 3.14 interpreter:

```sh
python3.14 evaluations/three_station/run.py REVIEWED_FULL_HARNESS_SHA /approved/catalog/fma/9423.mp3
```

Capture its own status, not a pipeline's last status; a nonzero exit is failure. The
coordinator records the concrete interpreter/path and command privately before launch.
`create_args` is the exact reviewed Docker argument builder: immutable local image,
`--pull never`, network none, non-root UID/GID 1000, read-only root, init, dropped ALL
capabilities, no-new-privileges, default seccomp, private cgroups, the unchanged limits,
no restart, only two read-only file mounts and no ports, writable volume or host socket.
The installed canonical bot entrypoint is replaced by this probe; no bot login occurs.
The randomized name and matching ownership label are printed before create. No image,
network, volume, shared service or unrelated container is created or modified.

## Receipts, failure and cleanup

Success requires all 12 ordered probe rows, exact case/frame/session counts, no unexpected
cleanup failures, zero retained banks/reservations, the intentional debt observations,
host exit status zero, OOM false and verified container removal. A final pass string alone
is insufficient. Preserve all partial sanitized rows and the first failed attempt.
Malformed, oversized, truncated, duplicate-field or payload-bearing output cannot pass.

The probe records actual kernel/page size, reservation totals, six-slot overlap, readiness
milliseconds, consumed frames, first-clock cleanup milliseconds, settled native owners,
retained debt before/after release, session/message counts, cgroup memory peak, sampled
task peak and final count, CPU usage and throttled time. At 20 ms sampling points it
rejects memory events, task-limit events and swap use. Sampling is not a complete task
peak measurement; CPU counters are observations, not a new CPU acceptance threshold.

Host cleanup starts one original **three-second** clock, including lookup, ownership
check, stop if still running, log collection, normal removal and absence check. It never
uses forced removal or pruning. Deadline expiry, failed commands, interruption or an
uncertain create retains the printed exact name as cleanup debt and cannot pass. Do not
retry or reuse the name. The coordinator checks that exact name/label, settles it through
supported stop and normal removal, verifies absence and preserves the original failed
verdict. Recovery after expiry does not turn the original cleanup into a pass.

## Evidence limits and offline checks

This is a short producer/allocator useful-load probe, not full release qualification.
It does not exercise `DiscordAdapter.play`, `StationSource`, matching, credits, voice
encoding/networking, Jamendo checked HTTP streaming, TLS, database restart or a complete
recording decode. Fourth-owner refusal here reaches the production pool boundary;
the existing `tests/test_station_capacity.py` tests reach the actual adapter command
boundary before interpretation and are reused, not represented as image execution.
Replacement here consumes real ready banks successively; adapter replacement, failed
preparation preservation and mode changes remain covered by their existing offline
regressions and need their own actual-image/voice evidence where applicable.

The deliberate held close is a controlled fault, not a claim that a stock decoder
normally takes more than three seconds. No total-RSS, complete copy-payload accounting,
excluded kernel backing/reclamation, 60-minute continuity, latency-to-listener, provider
quota, listening quality or fresh supply availability result is inferred. Historical
L2/L3 and packaging evidence keep their original bindings. These gaps remain on #212
and its parent outcome, rather than becoming new implementation gates.

Ordinary Linux tests import both scripts without invoking Docker, native decoding or
`target()`. They exercise real generated streams on loopback, real producer ownership
with a small Python child in place of mpg123, held-close debt, bounded reports and host
cleanup failure schedules. Run the targeted check with:

```sh
uv run pytest -q tests/test_three_station_evaluation.py
```

These checks plus locked sync, all pre-commit hooks, the full offline suite and the
post-push exact-commit check are implementation evidence only. Actual-image execution
and independent result review remain outstanding after the harness PR.
