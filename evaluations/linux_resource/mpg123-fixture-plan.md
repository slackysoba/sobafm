# First owned MP3 useful-fit check

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). **First decode incomplete:
exact counts and clean release observed, diagnostic expectations corrected.** This narrows the observation
to one of Q3's originally planned owned 120-second MP3 fixtures. It does not
replace the remaining VBR, 44.1 kHz, mono, overlap, FLAC, Q4 or native arm64 cases.
The [separate AS startup](mpg123-proposal.md#stricter-address-space-startup) passed;
its source and image binding remain separate from input execution.

## Owned fixture and development operation

Generate exactly 5,760,000 stereo sample frames at 48 kHz, signed little-endian
16-bit: 23,040,000 streamed raw bytes and 120 seconds. Both channels use an
integer-only triangular waveform with distinct periods and amplitude below
one quarter of full scale. The standard-library generator retains one 65,536-byte
buffer, feeds it completely before reuse and records its streaming SHA256. No
microphone, speaker, existing recording, catalog, network input or restricted
sample participates. No full raw recording is stored.

Use the existing reviewed FFmpeg image
`sha256:f214f61096a8823a587859e4ce87616f25a63f77b37e659c79ef8757a6cef76c`
and executable SHA256
`e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a`.
The fixed encoding command is:

```text
/usr/bin/ffmpeg -nostdin -hide_banner -loglevel error -n
  -fflags +bitexact -f s16le -ar 48000 -ac 2 -i pipe:0
  -map 0:a:0 -map_metadata -1 -c:a libmp3lame -threads:a 1
  -b:a 128000 -flags:a +bitexact -write_xing 1 -id3v2_version 0
  -f mp3 /fixtures/owned-cbr-48000-stereo-120s.mp3
```

The output is a fresh seekable regular file, so the muxer can finish the
Xing/Info duration and encoder-delay/padding information. Existing output must
fail rather than be overwritten. Keep the original raw frame count independent
of this metadata. Require encoder exit zero, all raw bytes accepted, bounded
diagnostics, a nonempty complete output no larger than 2,500,000 bytes, and actual
child/I/O release. A mismatch or error ends the one operation; do not silently
drop metadata, change sample-count expectations or retry encoding.
[libmp3lame options](https://ffmpeg.org/ffmpeg-codecs.html#libmp3lame),
[MP3 muxer](https://ffmpeg.org/ffmpeg-formats.html#mp3).

This is development fixture generation, with a separately reviewed container
cap of 512 MiB, zero swap, two CPUs and 64 tasks. It is not a runtime memory-fit
measurement. Retain UID/GID 1000, default seccomp, no capabilities,
no-new-privileges, private cgroups, network none and read-only root. Mount only
the evaluation sources read-only and one new, empty, project-owned fixture
directory writable. No installation, image build or additional cache is needed.
Set the encoder's hard/soft file-size limit to 2,500,000 bytes before exec and
disable core files. Bound encoding to 60 seconds and cleanup to one original
three-second deadline; retained ownership remains debt. Bound stderr to 65,536
bytes and selected JSON to 64 KiB. The host retains a 90-second operation and
90-second ownership-cleanup allowance with exact container identity checks,
normal stopped-container removal and absence verification. Fixtures stay within
the coordinator's 10 MB allocation and selected evidence within 5 MB.

## Recorded generation

The one released fixture-generation operation passed at source
`9935b16e142171100820103315f86e521d825aff`. It accepted exactly 23,040,000 generated
raw bytes, with SHA256
`0feb0dec8b6eb253893680342a58d1abb37727ef0da8f8aace849608ff41fa54`.
The retained MP3 is 1,920,768 bytes, SHA256
`1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d`.
The actual file/core limits were verified before exec; the encoder exited zero
with no stderr. It was reaped with no descendants or cleanup debt within the
original deadline. The container stopped with PID zero and no OOM kill, then was
removed normally and verified absent. Encoding plus in-container observation
took 6.435 seconds; the outer operation took 10.031 seconds. No decode or listening
followed at the generation stage. The subsequent first decode below observed the
exact hypothesized gapless output length, while retaining an incomplete verdict.
The [selected generation receipt](results/2026-10-27-owned-mp3-fixture.yaml)
preserves the command, source/image bindings, counts, hashes and release evidence.

## Native input boundary

Keep the audited fixed decoder command, with no quiet option:

```text
/usr/bin/mpg123 --no-control --no-visual --buffer 0 --stdout
  --rate 48000 --stereo --encoding s16 --resample ntom --no-resync -
```

Use the existing mpg123 image and executable binding. Set and verify hard/soft
AS 12,524,288, DATA 12,000,000 and STACK 524,288 before exec, with the same runtime
group/security configuration as the passing startup. Also set hard/soft CORE
to zero and require the observed core routing to be the plain `core` file pattern;
a piped handler would need separate assessment. No host setting is changed.
No stdin byte is available
to the old Python image. Before receiving input, the owner must observe the
complete fixed three-line startup banner on stderr, with the expected 1.33.7
version. In the exact source, `main` calls `print_title(stderr)` at line 1199;
its three writes are at lines 1561-1563. This occurs in native user code after
kernel exec setup, before decoder creation at 1215 and stream opening at 1356.
It is an existing program event, not an invented native handshake.
[mpg123 1.33.7 main](https://scm.orgis.org/mpg123/tags/1.33.7/src/mpg123.c).

After the banner, request SIGSTOP and require the child's actual stopped state
within the same preparation deadline. Verify PID/start identity, executable,
all resource limits, group/security identity, a single thread and the current
mapping inventory while input remains withheld. Record explicit vDSO/vvar and
x86 vsyscall scope exclusions; no sampled total substitutes for AS enforcement.
The exact WSL kernel defines vsyscall as a read/execute or execute-only kernel
gate VMA. It contains no PCM and supplies no numeric memory allowance.
[Kernel vsyscall mapping](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/arch/x86/entry/vsyscall/vsyscall_64.c).
Confirm no
unexpected descendant, backing file or output module. Resume only that owned
child after verification, then allow bounded fixture input. A missing banner,
stop/identity mismatch, exit or unexpected mapping ends the check and enters
owned cleanup. The complete mapping allowlist and executable-binding checks are
implemented in the opt-in [one-fixture harness](owned_mp3_decode.py) and require
independent review before execution. Its seven allowed files are the bound
mpg123 executable, dynamic loader, libc, libm, libmpg123, libout123 and libsyn123.
Other files, shared backing, executable anonymous mappings and writable kernel
special mappings are rejected. Whole-group process checks also retain debt for
unexpected processes outside the original decoder process group.

## First decode and capacity observations

The first check admits one native decoder address space. Its slot remains owned
through setup, execution, cancellation and cleanup debt. Before any later
two-decoder overlap, launch both children while the parent has no PCM; never
fork a populated PCM bank into an uncharged pre-exec image. At most two decoder
address spaces, including descendants and debt, are admissible across the proof.
The 64-task group limit does not implement that admission rule.

Allocate the existing two 2,304,000-byte banks and 4,194,304-byte copy reserve
only after the stopped native identity gate, touch every page to exercise the
full prepaid allocation, and retain them for the check. Before
resuming or feeding, snapshot the entire 1,920,768-byte compressed fixture into
the copy reserve using reads no larger than 65,536 bytes, then verify its exact
length and SHA256. Subsequent host-file changes cannot alter this admitted
snapshot. The read-only input view and 65,536-byte PCM read view occupy disjoint
parts of the reserve. Use `readv` and contiguous memoryview assignments to avoid
additional Python PCM byte strings. Use bounded reads,
memoryviews and a fixed bank; do not accumulate decoded output or write it to
disk. Account every live parent PCM-bearing temporary within the copy reserve.
Read fixture input in chunks no larger than 65,536 bytes and cap stderr at
65,536 bytes. A drain sink discards counted PCM after verifying 4-byte frame
alignment, rather than playing or exporting it. Kernel pipe storage is bounded
and explicitly classified under the accepted exclusions/reservations.

Require six seconds of ready PCM (1,152,000 bytes) within 15 seconds from launch,
then exactly 23,040,000 output bytes for this known 48 kHz stereo fixture. At
native 48 kHz the selected command uses 1:1 synthesis, avoiding NtoM rate-ratio
quantization in this first check. Preserve output count/digest, first-readiness
time, total time and diagnostics. Transfer completion, decoder exit, exact known
PCM count and bounded diagnostic policy are distinct predicates; exit zero
alone never proves complete audio. Establish and review the exact expected
banner/progress/completion diagnostics before accepting any output result. The
harness requires the fixed banner, one `Note: forcing output encoding s16`,
one playing line, the exact `MPEG 1.0 L III vbr 48000 j-s` line for this fixture,
and one completion line; only blank
lines and one bounded directory line may accompany them. Unknown diagnostics,
repeated markers or a format change fail even with exit zero.
[Compact native format output](https://scm.orgis.org/mpg123/tags/1.33.7/src/common.c).

Use a 150-second overall workload bound, with no OOM event, no unexpected child
and one original three-second cleanup deadline covering pipe closure, TERM/KILL,
reaping and reference release. Failure keeps debt/slots until actual release;
late release cannot convert a missed deadline into a pass. Preserve safe cgroup
event deltas, final container state and external ownership evidence. Do not
claim Q3, production resampler quality, malformed-input handling, Q4 or release
readiness from this one fixture.

The external operator mounts only evaluation sources and the single fixture
read-only, validates the exact committed harness/image/ELF/fixture identities,
and retains a 180-second operation plus 60-second ownership-cleanup allowance.
Attach is bounded to 165 seconds, selected stdout/stderr to 500 KB and the new
safe evidence directory to 1 MB. PCM is never emitted in that output. Preserve
failure and partial evidence; remove only the positively owned, stopped container
normally and verify absence. No automatic retry, limit tuning or continuation
to other native cases follows this one observation.

## First decode observation and diagnostic correction

The single released run at source
`4661132db02caf1c0cc7a912436b4a2f10fe1901` remains **incomplete** in the
[selected original receipt](results/2026-10-28-owned-mp3-decode-first.yaml).
It accepted all 1,920,768 compressed bytes and produced exactly 23,040,000 PCM
bytes with decoder exit zero. It held 1,155,072 ready bytes at 0.068161 seconds,
after the verified stopped native gate and full prepaid pool allocation.
The one native process had 4,448,256 ordinary mapped bytes at that gate; that
observation is not a peak or a substitute for the enforced AS bound. No memory
event or swap use occurred. Reaping, payload release and the original cleanup
deadline succeeded; the external owner removed the stopped container and
verified absence. No PCM was stored or played.

The original matcher rejected the expected encoding note and the `vbr` format
label. The fixed `--encoding s16` option prints that note in
[audio.c, lines 392-395](https://scm.orgis.org/mpg123/tags/1.33.7/src/audio.c).
A subsequent read-only scan of the unchanged hash-bound fixture found 5,002
consecutive 384-byte MPEG 1 Layer III frames through exact EOF, all with
128 kbit/s, 48 kHz and no padding bit: one stereo Info frame and 5,001
joint-stereo audio frames. Its `Lavf lame` extension has method nibble zero,
which mpg123 maps to `MPG123_VBR` even after an Info marker in
[parse.c, lines 315-330](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/parse.c).
This diagnostic therefore does not establish variable frame bitrate. The
5,001 advertised audio frames, less 576 samples of delay and 576 of padding,
give the independently known 5,760,000 sample frames.

The narrow matcher correction requires exactly one encoding note and the
observed format line. Its regression uses the exact retained stderr digest
and rejects missing/repeated encoding notes, changed encoding/rate/mode/bitrate
labels, unknown errors and missing/repeated completion. Counts, capacities,
command and deadlines are unchanged. The original incomplete result is not
reclassified. One corrected confirmation remains subject to exact review and
release; no further Q3/Q4 case is implied.
