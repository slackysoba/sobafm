# First owned MP3 useful-fit check

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). **Preparation only;
fixture generation and decoding have not run.** This narrows the next observation
to one of Q3's originally planned owned 120-second MP3 fixtures. It does not
replace the remaining VBR, 44.1 kHz, mono, overlap, FLAC, Q4 or native arm64 cases.
The [separate AS startup](mpg123-proposal.md#stricter-address-space-startup) passed;
its source and image binding remain separate from future input execution.

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

## Native input boundary

Keep the audited fixed decoder command, with no quiet option:

```text
/usr/bin/mpg123 --no-control --no-visual --buffer 0 --stdout
  --rate 48000 --stereo --encoding s16 --resample ntom --no-resync -
```

Use the existing mpg123 image and executable binding. Set and verify hard/soft
AS 12,524,288, DATA 12,000,000 and STACK 524,288 before exec, with the same runtime
group/security configuration as the passing startup. No stdin byte is available
to the old Python image. Before receiving input, the owner must observe the
complete fixed three-line startup banner on stderr, with the expected 1.33.7
version. In the exact source, `main` calls `print_title(stderr)` at line 1199;
its three writes are at lines 1561-1563. This occurs in native user code after
kernel exec setup, before decoder creation at 1215 and stream opening at 1356.
It is an existing program event, not an invented native handshake.
[mpg123 1.33.7 main](https://scm.orgis.org/mpg123/tags/1.33.7/src/mpg123.c).

After the banner, request SIGSTOP and require the child's actual stopped state
within the same preparation deadline. Verify PID/start identity, executable,
all three limits, group/security identity, a single thread and the current
mapping inventory while input remains withheld. Record the explicit vDSO/vvar
scope exclusion; no sampled total substitutes for AS enforcement. Confirm no
unexpected descendant, backing file or output module. Resume only that owned
child after verification, then allow bounded fixture input. A missing banner,
stop/identity mismatch, exit or unexpected mapping ends the check and enters
owned cleanup. The complete mapping allowlist and executable-binding checks
must be implemented and reviewed before this plan is executable.

## First decode and capacity observations

The first check admits one native decoder address space. Its slot remains owned
through setup, execution, cancellation and cleanup debt. Before any later
two-decoder overlap, launch both children while the parent has no PCM; never
fork a populated PCM bank into an uncharged pre-exec image. At most two decoder
address spaces, including descendants and debt, are admissible across the proof.
The 64-task group limit does not implement that admission rule.

Allocate the existing two 2,304,000-byte banks and 4,194,304-byte copy reserve
only after native input gating, and retain them for the check. Use bounded reads,
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
banner/progress/completion diagnostics before accepting any output result.

Use a 150-second overall workload bound, with no OOM event, no unexpected child
and one original three-second cleanup deadline covering pipe closure, TERM/KILL,
reaping and reference release. Failure keeps debt/slots until actual release;
late release cannot convert a missed deadline into a pass. Preserve safe cgroup
event deltas, final container state and external ownership evidence. Do not
claim Q3, production resampler quality, malformed-input handling, Q4 or release
readiness from this one fixture.
