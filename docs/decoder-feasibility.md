# Catalog decoder offline feasibility evidence

This is the 2026-10-07 preparation evidence for [#118](https://github.com/slackysoba/sobafm/issues/118) and [Proposed ADR-0007](decisions/0007-bound-catalog-audio-decoding.md). It extends [the existing candidate survey](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6032402289) with bounded synthetic conversion/failure measurements. It selects no runtime decoder and establishes no license/security exception. #116's actual catalog evidence and live measurement trigger remain outstanding.

## Artifact and tested platform

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

## Actual results

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

## Retained evidence and next work

The small [measurement record](decoder-feasibility-results.json) contains sanitized final metrics, fixture digests and exact arguments. Absolute local paths and raw native error addresses are excluded. Scratch evidence is retained under `%TEMP%/sobafm-decoder-118-20261007-9b8a54`; the production tree contains neither the binary, fixtures nor throwaway harness.

| Retained scratch file | SHA-256 |
| --- | --- |
| `evaluation-v3.py` | `e3f5193e28748a1bdcae45ed6dc83baa720c256d21eb6eccf2a0718205aeaede` |
| `results-v3.json` | `5c014b229097ceda24a6fec3ece05ba8b2008b118ef39bb43a01e96842e8083f` |
| `evaluation-v3.log` | `6b4af21810bfe12047993d25a3272d147aa16fa620288a898a174b10ce2ed234` |
| `version-license.txt` | `1d8dc9f950a5a656e745ab0fee9baf67862e05d09ffea258c735e60b97b0e383` |

The proposal's [adoption gates](decisions/0007-bound-catalog-audio-decoding.md#consequences) assign the remaining #116 measurements to its operator/maintainer, licensing/build/cap choices to the maintainer on #118, and actual three-platform/native-memory/image qualification to a build evaluator after those choices. The primary coordinates those gates and independent review. Runtime integration remains gated on accepted decisions.
