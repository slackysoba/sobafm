# Catalog decoder feasibility evidence

Evidence refreshed 2026-10-15 for [#118](https://github.com/slackysoba/sobafm/issues/118) and [ADR-0007](decisions/0007-bound-catalog-audio-decoding.md), which takes effect on merge. The original 7 October Windows control and its measurement record are preserved below. Later sections reconcile the separate 8 October C1 Linux assessments, D1 metadata and 13 October qualitative research acceptance. The [candidate survey](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6032402289) remains dated evidence, not a new survey. The maintainer [selected the supervised FFmpeg approach on 15 October](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6193735484); ADR-0007 selects the minimal LGPL supply, containment, accounting and completion contracts. Its architecture acceptance is separate from missing implementation qualification. No binary, platform or runtime pass follows from these historical measurements.

## Original Windows control: artifact and tested platform

- **Executed:** Windows 11 x86-64, build 26200; CPython 3.14.7, `ProactorEventLoop`. One local machine; no Linux execution or container-image measurement.
- **Provider:** [BtbN dated release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-07-13-07), provider tag commit `9acad4a9ef1583096af7836cc1e9c8cbcb4d3950`.
- **Archive:** `ffmpeg-n9.0.2-22-g46d8f462ee-win64-lgpl-shared-9.0.zip`, 77,229,626 downloaded bytes; SHA-256 `7b3207b46b1472142aaef0f45a1093bfa861aa43dae605567ac6244d8f3296a4`, checked against the release asset digest **before extraction/execution**.
- **Executable:** `n9.0.2-22-g46d8f462ee-20261007`; reports LGPL version 3 or later, `--enable-version3 --enable-shared --disable-static`, without `--enable-gpl` or `--enable-nonfree`. The archive's `LICENSE.txt` contains LGPLv3. This is not a complete linked-component source/license review.
- **Unpacked archive:** 191,014,221 bytes, including ffmpeg/ffplay/ffprobe, shared libraries and documentation. `ffmpeg.exe` is 540,160 bytes and `avcodec-63.dll` is 91,137,024 bytes. Neither the executable size nor this archive total is an installed Linux image delta; no minimal runtime subset was qualified.
- **Metadata only:** that dated release lists LGPL shared Linux x86-64 and ARM64 archives (63,539,308 and 54,083,504 download bytes). They were not downloaded or executed. Their metadata is not evidence that installation, codecs or lifecycle tests pass there.

The binary, generated fixtures and three versions of a disposable measurement harness remain together in local scratch, outside every repository checkout. Nothing was globally installed, added to `PATH`, added to SobaFM's environment/lockfile or distributed. No user/music recording, catalog stream, Gemini/Lyria/Jamendo call or application startup was used.

## Reproduction and measurement method

The fixtures were generated with the evaluated FFmpeg's `lavfi` `aevalsrc`, amplitude 0.1, sample rate 44,100 Hz, then `libmp3lame` at 128 kbit/s. A 12-second mono tone uses 440 Hz. A 240-second stereo tone uses independent 440 Hz left and 880 Hz right signals. Both are newly generated noncopyrighted test tones.

For example, the long fixture input expression is:

```text
aevalsrc=0.1*sin(2*PI*440*t)|0.1*sin(2*PI*880*t):s=44100:d=240
```

Generate an MP3 file in scratch with `-f lavfi -i <expression> -c:a libmp3lame -b:a 128k -f mp3 <scratch-output>`. No production audio is written. Use the verified absolute executable, a fixed argument array without a shell, and this decode argument sequence:

```text
-hide_banner -nostdin -loglevel error -xerror -max_alloc 16777216
-threads 1 -protocol_whitelist pipe -f mp3 -i pipe:0
-map 0:a:0 -vn -sn -dn -threads 1
-ar 48000 -ac 2 -c:a pcm_s16le -f s16le pipe:1
```

The scratch harness feeds the generated file in 16,384-byte chunks, awaiting drain after each write. It concurrently drains stdout in reads of at most 3,840 bytes and stderr in 4,096-byte chunks. The PCM consumer hashes/discards output through a queue limited to fifty chunks, rather than retaining a track. The long-stream consumer waits 1 ms after each twenty-five reads to exercise backpressure; cancellation/forced-exit consumers wait 50 ms after each read. These are accelerated synthetic measurements, not real-time playback or underrun tests.

`asyncio.create_subprocess_exec` used reader `limit=16,384`. Reader and writer buffer lengths were sampled via private attributes solely for scratch instrumentation, not proposed as a supported production API. The queue's peak is tracked after each insertion. A separate fixed 192,000-byte PCM analysis window verifies channel/frequency conversion; its storage is **additional measurement overhead**, not part of the reported queue. Fixtures are on disk and at most one 16 KiB input chunk is read by the feeder; fixture-hash calculation happens after decoding and is not a streaming-memory measurement.

Child memory is observed via [`GetProcessMemoryInfo`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getprocessmemoryinfo) and [`PROCESS_MEMORY_COUNTERS_EX`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex), with a retained process handle and 5 ms requested polling. `PeakWorkingSetSize` and `PeakPagefileUsage` are OS peak counters; `PrivateUsage` is sampled current private commit. Polling cadence varied with Windows scheduling. Parent interpreter RSS, kernel pipe storage, native audio allocations, concurrent decks and hard OS limits were **not** measured. Working set includes code/shared pages; it cannot identify how much memory is audio.

Every case has a bounded deadline (30 seconds, except the 0.25-second idle test). Deliberate cancel/kill occurs 0.35 seconds after spawn. The final harness cancels/joins every owned task, terminates the child, closes stdin, drains/discards remaining output/errors to EOF in bounded chunks and waits for exit. It ran with `ResourceWarning` promoted to errors and completed without a warning/traceback. An earlier long run reached its 30-second deadline with per-read pacing; early harness versions also exposed unclosed parent pipe transports. Those attempts are preserved, and neither is counted as a successful complete-stream or clean-lifecycle result.

## Original Windows control: actual results

The table records the final corrected run. PCM byte counts are bytes accepted by the sample counter before the boundary, including a current read waiting for queue space; a cap-crossing read is excluded. They are not padded Discord frames. Timing describes this host/run only; there is no universal latency or memory guarantee.

| Case | Encoded bytes fed | PCM bytes counted | Actual outcome |
| --- | ---: | ---: | --- |
| 12-second mono | 193,140 | 2,307,340 | Exit zero in 0.109 s; 576,835 stereo samples; both channels carry 440 Hz. |
| 240-second stereo, bounded consumer | 3,841,087 | 46,082,716 | Exit zero in 2.610 s; 11,520,679 stereo samples; distinct 440/880 Hz channels retained. |
| 65,536 zero bytes | 65,536 | 0 | Forced MP3 parser rejects input; exit 3,199,971,767 in 0.127 s; 269 stderr bytes. |
| Synthetic RIFF/WAVE-like header | 1,040 | 0 | Forced MP3 parser rejects input; same nonzero exit in 0.094 s. This was an invalid header, not a valid WAV compatibility test. |
| First 32,000 bytes of long MP3 | 32,000 | 376,368 | **Exit zero**, no stderr, only 94,092 stereo samples (1.960 s). `-xerror` does not detect this shortened valid stream. |
| Artificial 32,768-byte input cap | 32,768 | 0 | Next chunk rejected before write; child terminated/reaped 0.0068 s after boundary. |
| Artificial 384,000-byte PCM cap | 163,840 | 382,804 | Next read rejected before delivery; child terminated/reaped 0.0146 s after boundary. Already generated pipe bytes are discarded, not retained. |
| Open stdin without any input | 0 | 0 | 0.25 s deadline; process reaped 0.0110 s after boundary, 0.289 s total including spawn/scheduling/cleanup. |
| Cancel backpressured decode | 163,840 | 182,184 | Cancellation caught; process reaped 0.0114 s after cancel. |
| Force child termination | 163,840 | 182,496 | Broken-pipe/exit path contained; process reaped 0.0230 s after kill. This simulates abrupt exit, not a native exploit/crash test. |
| Healthy 12-second decode after failures | 193,140 | 2,307,340 | Exit zero; PCM digest identical to first healthy decode; parent remains usable. |

For the complete 240-second case:

- PCM queue peak **188,240 bytes** (configured ceiling 192,000); stdout reader sampled peak **52,508 bytes**; write transport peak **81,920 bytes**; one application read at most **3,840 bytes**, plus the feeder's 16 KiB chunk. No whole-track PCM object exists. Kernel pipe bytes and the analysis window are outside those counters.
- Child peak working set **21,745,664 bytes**; peak process commit **67,661,824 bytes**; sampled private commit peak **20,725,760 bytes**. The difference reinforces that observed working set is not a total-memory ceiling. Maximum child working set across final cases was **22,159,360 bytes**.
- Left-channel 440 Hz amplitude is about 3,113 s16 units with 880 Hz leakage about 0.026; right-channel 880 Hz about 3,113 with 440 Hz leakage about 0.023. This checks rate/channel/sample interpretation on simple tones, not listening quality, clipping or a resampler quality benchmark.
- Output is aligned to four-byte stereo samples but leaves a **2,716-byte** remainder in a 3,840-byte playback frame. The mono fixture leaves **3,340 bytes**. A verified EOF therefore needs explicit final-frame handling; short pipe reads must also be assembled.

The event-loop heartbeat's largest observed gap across final cases was about 30 ms. There was no Discord player/mixer in this harness, so that observation does not prove playback-thread latency or NFR-2. No OS Job Object/cgroup limit, OOM event, hostile corpus, full cancellation matrix, real-time crossfade or platform concurrency qualification was run.

## Selected C1 Linux assessment and remaining qualification

The maintainer [selected minimal LGPL FFmpeg assessment only](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6064977166). M118-LGPL-9.0.2-C1 uses official FFmpeg 9.0.2 commit `946fcce07b6dcd0331c8cc609192aeff5e1924f8`, source archive SHA-256 `c8a74e6eff0d38850403ee8eff3a95721984b45a3cd25f92b8905886571fc1be`. The requested MP3float/MPEG parser/MP3 demux/PCM s16le/pipe configuration uses shared libraries, disables network/GPL/nonfree/version3, and targets LGPL-2.1-or-later. It is distinct from the older broad Windows LGPLv3 control above; its executed platform was Linux AMD64 under a non-root, network-disabled assessment container.

| Completed assessment | Observed result and limit |
| --- | --- |
| [Initial build and static review](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6066397238) | Exact upstream tree and installed hashes/configuration/imports checked. Initial RUNPATH was wrong; the failure is retained, not an install pass. Six original ELF files totalled 2,396,784 bytes excluding runtime dependencies/notices/source/packaging; this is not an image delta or total footprint. |
| [Behavior](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6066661962) | Eleven candidate cases plus two separate TERM-resistant controls; full mono/stereo yielded 2,307,340/46,082,716 PCM bytes and healthy recovery matched. This earlier package used an explicit loader path. Truncated MP3 exited zero after 1.96025 seconds; frame remainders still need assembly/EOF policy. |
| [Corrected relocation and smoke](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6067186434) | Rebuilt archive SHA-256 `3ca5e16f1219b6f2d344bc201828a7ef332da3e4ebe04db5bb63627ac8514d02`; all 182 installed hashes checked. Startup/own-library resolution succeeded after relocation, including a path with spaces, without loader overrides; missing-library negative control failed as expected. One mono smoke matched the prior digest. This was not a repeat of the full matrix or clean-host qualification. |
| [Source/support/notice inventory](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6068002879) | Source/build/install/dependency/support/notice records independently matched. IJG/ISC/MIT/zlib-derived material and compiler/runtime support remain relevant. Matching GCC/glibc originals and Debian patches are retained; descriptor signatures and installed source-to-object mapping remain unverified. Full shipped-runtime SBOM, recipient rebuild/replacement and durable source/update/distribution ownership remain open. |
| [Limited resource observations](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6068002879) | Six cases under an experimental aggregate 128 MiB container envelope recorded zero memory/OOM/PID event changes. Cumulative charge peak 29,036,544 bytes includes setup/supervision. A separate touched-allocation control reached 134,217,728 bytes with child exit -9 and one OOM kill; supervisor exit zero was enforcement evidence, not workload success. This is not strict per-child, ordinary-source, whole-host, native-audio or NFR-3 qualification. |
| [Focused evaluator correction](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6068937334) | Original shield/gather diagnostic and results retained. Corrected structured-concurrency controls joined 13 tasks; focused unchanged-candidate idle/cancellation/golden-mono confirmation joined 24 tracked tasks and nine cleanup futures, with zero final diagnostics after quiescence/shutdown/GC. This closes only the demonstrated evaluator gap for those paths, not every external-caller cancellation/error/cleanup interruption or production lifecycle. |

These completed observations need no unchanged build, broad resource study or relocation smoke rerun. Linux ARM64 native execution, a selected C1 Windows build, compiler-free clean installation and ordinary source/container containment remain unqualified. Neither the Windows control nor container metadata fills those gaps. Image/layer delta, exact resource enforcement semantics, native audio reservations and all-source accounting remain separate from measured executable size or aggregate charge.

## Research disposition and catalog technical evidence

M4's prerequisite is recorded by the [stable v1.0.0 delivery disposition](https://github.com/slackysoba/sobafm/issues/4#issuecomment-6097955604), with its specific exceptions and incomplete/unmeasured checks preserved. It is not a decoder or broader NFR pass.

The [D1 outcome](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104976415) ran on source `a1e463903fa289f215ad2fd8d0d106471f39db3e`: all 48 cases executed and classifications passed, yielding 189 track appearances/180 distinct IDs. Thirty-eight of all 42 music cases returned at least three tracks; four returned none and remain in the denominator. Report file SHA-256 is `0f29ddc0d1dcb143011917b661364cf400b8c54e66835590c5bde1d0b39a41c4`; canonical report digest is `56f20ba83f405db084dfae3ad381518ac4142e050ef218442e6e74a4db8ef1e8`. Earlier partial/fatal reports remain separate; their original causes are unproven.

D1 recorded 378 stream observations: mp31 had 189 successes; mp32 had 126 successes and 63 opaque errors. The 315 successes retained single HTTP 200 HEAD observations; opaque errors retained no status/length, and false flags prove no absence. Missing content length stays unavailable. Stream labels and HEAD metadata do not prove an audio GET, actual MP3 bitstream/sample rate/channels, decoded completeness, playback or the suitability of the candidate 16 MiB/900-second limits. No audio was fetched, decoded or played in D1.

Across the 189 track appearances, duration metadata ranges from 16 to 2,111 seconds, with two appearances above the candidate 900-second cap. The HEAD size metadata is:

| Format | Known lengths | Unavailable | Known byte range | Appearances above candidate 16 MiB |
| --- | ---: | ---: | --- | ---: |
| mp31 | 189 | 0 | 198,348–25,364,992 | 2 |
| mp32 | 126 | 63 | 666,487–37,842,699 | 1 |

These are appearance counts, not unique tracks, catalog prevalence or filtered/license-eligible coverage. Duration and size outlier counts may overlap and are not summed. ADR-0007 selects the 16 MiB/900-second upper guardrails knowing they exclude some of this returned sample on metadata checks; a useful qualified operating envelope remains required before catalog readiness. HEAD sizes are not observed GET byte counts, and the unavailable mp32 lengths remain unknown. No cap, delivery or decoder-memory qualification follows.

On 13 October the maintainer [accepted the research premise qualitatively and waived structured listening/export and calculated go/no-go](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6145275917), closing #116. The original deadline, 2026-10-12T23:40:29.544021Z, was missed and is not reset. Preserved ratings remain null; the official frozen summary remains `complete=false` / `recommendation=pending`. The original numerical criterion, 28 of 42 music cases with three fitting tracks in the top five, remains unmeasured. The maintainer's first-ten weak-fit feedback feeds #117/#126 without per-track scores or agent hearing. This resolves research disposition, not decoder/platform/distribution/legal acceptance or later filtered catalog quality.

## Station-wide interpretation

These decoder-only measurements do not establish a station-wide PCM ceiling or available native-memory headroom. ADR-0004's historical estimate of three nominal 60-second buffers gives 34,560,000 PCM bytes and a nominal 5,440,000-byte difference from NFR-3; neither is a demonstrated current worst-case allowance. The [supplemental independent station review](https://github.com/slackysoba/sobafm/pull/157#pullrequestreview-5449091514) reports four retained nonempty Lyria buffers of 8/60/2/2 seconds with two open producers, totaling 13,824,000 PCM bytes. That fixture does not demonstrate an NFR-3 overrun. The selected byte guardrails require implementation and qualification across all retained/retired/spare/filling/incoming sources, receive overshoot and transitions between modes while preserving current Lyria behavior. Any material revision is recorded under the delegated authority; an unresolved bound keeps catalog unavailable.

This corrects the proposal's interpretation of station admission and historical arithmetic. The decoder experiments were not rerun, and their measured results, committed JSON and retained raw evidence are unchanged.

## Retained evidence and next work

The small [measurement record](decoder-feasibility-results.json) contains sanitized final metrics, fixture digests and exact arguments. Absolute local paths and raw native error addresses are excluded. Scratch evidence is retained under `%TEMP%/sobafm-decoder-118-20261007-9b8a54`; the production tree contains neither the binary, fixtures nor throwaway harness.

| Retained scratch file | SHA-256 |
| --- | --- |
| `evaluation-v3.py` | `e3f5193e28748a1bdcae45ed6dc83baa720c256d21eb6eccf2a0718205aeaede` |
| `results-v3.json` | `5c014b229097ceda24a6fec3ece05ba8b2008b118ef39bb43a01e96842e8083f` |
| `evaluation-v3.log` | `6b4af21810bfe12047993d25a3272d147aa16fa620288a898a174b10ce2ed234` |
| `version-license.txt` | `1d8dc9f950a5a656e745ab0fee9baf67862e05d09ffea258c735e60b97b0e383` |

The decision's [implementation and qualification gates](decisions/0007-bound-catalog-audio-decoding.md#consequences) distinguish chosen contracts from unfinished evidence. The minimal C1-derived LGPL route requires SobaFM-owned versioned source/notices and updates, mandatory pre-input OS containment, and strict HTTP/duration/EOF completion that rejects missing duration and uses a one-second allowance. Exact shipped-runtime/source/SBOM/replacement and distribution access, compiler-free supported platforms, native/transport resources and that completion allowance still require evidence. The [reviewed shared audio contract](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6193982126) reserves future Lyria growth and retains cleanup debt; it proves neither a hard baseline Lyria bound nor a nonzero catalog allowance. ADR-0007 accepts the architecture and upper guardrails on merge, followed by #119 and dependent implementation. [#174](https://github.com/slackysoba/sobafm/issues/174) owns native supply and [#175](https://github.com/slackysoba/sobafm/issues/175) owns supported-platform and combined-resource qualification before catalog readiness or shipping. Zero decoder exit is insufficient; six-second streaming readiness may expose a prefix before later failure, while a shorter source must first pass full strict completion. Normal performance completion also requires its buffered PCM to drain. Every catalog-involving boundary is successive without a catalog fade or overlap; generated-to-generated behavior remains unchanged. The three-second cleanup deadline covers every owned resource and retains debt, reservations and slots until actual release. Historical JSON and other candidate limits are preserved; the cleanup contract is explicitly revised. This update changes no historical measurements and runs no new decoder, audio or provider experiment.
