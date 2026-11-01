# Owned VBR MP3 at 44.1 kHz mono

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). **Preparation only; no
VBR fixture generation or decoding has run.** Reuse the passing retained CBR
and [two-decoder overlap](mpg123-overlap-plan.md#recorded-overlap-result) evidence.
This slice supplies the remaining MP3 bitrate/rate/channel case from Q3. It
does not include FLAC, Q4, source audio, provider/Discord activity, listening,
native arm64 or playback architecture adoption.

## One bounded generation

The existing [generator](owned_mp3_fixture.py) has two closed recipes. Its
original CBR recipe is unchanged; do not regenerate it. The new recipe supplies
exactly 5,292,000 mono signed 16-bit little-endian samples at 44,100 Hz: 120 seconds
and 10,584,000 raw bytes. A single 65,536-byte reusable buffer contains deterministic
integer triangles with periods 100, 137, 200 and 333 samples in successive
five-second segments. The sequence repeats every twenty seconds. Amplitudes stay
between -8,000 and 8,000; there is no randomness, floating state or raw PCM file.

Use the retained encoder image and executable from the
[original fixture](mpg123-fixture-plan.md): image
`sha256:f214f61096a8823a587859e4ce87616f25a63f77b37e659c79ef8757a6cef76c`,
FFmpeg ELF `e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a`.
No pull, build, package installation or source archive is needed. The fixed
command is:

```text
/usr/bin/ffmpeg -nostdin -hide_banner -loglevel error -n -fflags +bitexact
  -f s16le -ar 44100 -ac 1 -i pipe:0 -map 0:a:0 -map_metadata -1
  -c:a libmp3lame -threads:a 1 -q:a 4 -flags:a +bitexact
  -write_xing 1 -id3v2_version 0 -f mp3
  /fixtures/owned-vbr-44100-mono-120s.mp3
```

FFmpeg's [libmp3lame documentation](https://ffmpeg.org/ffmpeg-codecs.html#libmp3lame)
maps the command-line quality option to LAME's VBR quality selection. The option
does not by itself establish that this file varies its audio-frame bitrate;
the complete header inspection below must establish that separately.

Generation runs only after independent review of committed sources, the exact
operator and the current binary/advisory evidence. The retained image binding
does not replace that review. Use a fresh empty project-owned output directory, mounted
writable at `/fixtures`; evaluation sources are read-only. Keep the original
development-only 536,870,912-byte container cap and no swap, two CPUs, 64 tasks,
UID/GID 1000, default seccomp, no capabilities or new privileges, no network,
read-only root, init and no restart. These are encoder preparation controls,
not the decoder runtime-fit configuration or a change to its guarantee.

The complete native `execve` environment remains the literal `CHILD_ENV` in
[decoder_startup.py](decoder_startup.py): `PATH=/usr/bin:/bin`, `LANG=C`,
`LC_ALL=C`, `HOME=/nonexistent`. It inherits no `LANGUAGE`, catalog, conversion
or loader override. For the current advisory review, these facts exclude the
reported non-C locale plus attacker-controlled `LANGUAGE` precondition. Fixed
numeric PCM encodings, stripped source metadata, literal argument arrays,
binary open modes and an authenticated owned input expose no user-selected
EUC/SHIFT_JISX0213 conversion, monetary format, `fopen` mode, shell word
expression, DNS debugging request or million-node search-tree input. These are
narrow input/path assessments; linked libc still contains other facilities,
and the image has not been patched or generally cleared. The
[central review](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6551417205)
accepts the known-trigger exclusions for this fixed owned workload, conditional
on the actual source, image and launch controls. It does not release execution
before review of the committed procedure and passing applicable checks.

The child must expose hard/soft file-size limit 2,500,000 and CORE zero before
its exec gate opens. Encoding has one 60-second deadline. Every partially
created pipe remains owned if setup fails. Original cleanup covers descriptor
closure, child/group reaping and parent payload-view release in three seconds;
failed closure retains descriptor debt. Require all input accepted, exit zero,
empty bounded stderr, no debt and a timely release before inspecting the file.

## Header-derived completion

The [closed structural checker](mp3_vbr_contract.py) scans the complete bounded
compressed file. Every frame must be MPEG1 Layer III, 44,100 Hz, mono and without
CRC protection, using a nonfree, nonreserved bitrate. Require a complete Xing
header, all four expected fields and the same `Lavf lame` encoder tag used by
the retained encoder. The advertised frame and byte counts must equal the
complete scan. Reject leading/trailing tags, truncated frames, format changes
and a file whose audio frames all use one bitrate. A different bitrate in the
Xing metadata frame does not establish VBR.

Record actual audio-frame count `F`, encoder delay `D` and padding `P`. They must
satisfy `1152 * F - D - P = 5,292,000`. This is header consistency, not decoding
or audio-quality evidence. Authenticate the entire generated file and retain
the raw-input hash, encoded hash, exact size, header fields and observed bitrate
set. Any failure ends the attempt with its file and evidence preserved.

The pinned mpg123 NtoM implementation uses integer phase, so do not substitute
the ideal floating sample-rate ratio. These exact version-specific sources
define the output-count predicate:

- [ntom.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/ntom.c),
  `INT123_synth_ntom_set_step` and `INT123_ntom_ins2outs`: scale 32,768, initial
  phase 16,384 and step `floor(48000 * 32768 / 44100) = 35,665`.
- [frame.h](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/frame.h),
  `GAPLESS_DELAY`: 529 input samples.
- [frame.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/frame.c),
  `INT123_frame_gapless_init` and `INT123_frame_gapless_realinit`: add synthesis
  delay, then convert the start and end endpoints separately.
- [synth_ntom.h](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/synth_ntom.h),
  `MONO2STEREO_NAME`: duplicate each mono sample into both output channels.

For `C(n) = floor((16384 + 35665 * n) / 32768)`, output sample frames must equal
`C(1152 * F - P + 529) - C(D + 529)`. For example, if the generated header has
`F=4595`, `D=576`, `P=864`, the expected output is 5,759,862 stereo sample frames,
or 23,039,448 bytes. These header values are examples until observed. The result
is 138 frames below the ideal 5,760,000; this follows from converter quantization
and is not a completion tolerance or a fidelity claim.

## Separate decoder release

Successful generation does not launch a decoder. First bind its actual hash,
size, header-derived exact output count and the decoder procedure to committed
sources; obtain independent review of that exact procedure. Reuse the retained
mpg123 image, fixed command, executable/loader closure, stopped native input
gate, AS/DATA/STACK limits and 90,177,536-byte whole-group cap from the passing
single-decoder case. No limit tuning or new binary is included.

Fork before any parent PCM allocation. After the native gate, allocate and
page-touch the same two 2,304,000-byte banks and 4,194,304-byte copy reservation.
One authenticated compressed snapshot occupies at most its first 2,500,000
bytes. The 65,536-byte output read view starts at offset 3,000,000, and the final
four reserved bytes hold partial stereo-frame carry. These partitions do not
overlap and introduce no additional PCM buffer.

Require six seconds of whole-frame readiness (1,152,000 bytes) within fifteen
seconds of fork, and complete input/output under the original 150-second workload
deadline. The streaming mono predicate compares both channels byte-for-byte,
including reads splitting either sample or stereo frame, and rejects partial
final frames, all-zero output, mismatches and the wrong complete frame count.
All carry bytes remain in the reserved view. Preserve the PCM hash without
storing or listening to decoded output. This is a bounded owned-fixture check,
not a general file-integrity or fidelity policy.

Keep the source-bound diagnostic policy. The expected compact format changes
to `MPEG 1.0 L III vbr 44100 mono`; the native banner, forced-s16 note, playing
and completion records remain required, and unknown or repeated messages fail.
The selected forced-rate NtoM path excludes the optional libsyn123 resampler;
its [audio.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/audio.c) forced output
rate also avoids the fallback NtoM warning branch. Record any unexpected actual
diagnostic as a failure instead of broadening acceptance during execution.

## External ownership, storage and stopping

Each stage gets a fresh name and evidence directory and pins its committed
source, actual mounted hashes, existing image and executable. Verify the full
container ID, exact labels, two intended mounts and every runtime/log control
before start. Do not infer ownership from a name. Generation permits only its
fresh fixture directory writable; decoding permits only that one fixture file
read-only. Nothing else is mounted.

Use the reviewed overlap operator's ownership behavior: one original external
three-second deadline starts at first attach termination, failure or cancellation
and covers original CLI/reader joins, stop, inspection, normal removal and
verified absence. Repeated cancellation cannot renew it. Late actual release
clears debt but remains incomplete. Unknown creation remains owned even after
an empty name lookup. Recovery never starts another workload or abandons an
original CLI/reader. The 240-second retention backstop reports failure while
the owner continues bounded management recovery.

Bound each stage to 180 seconds overall and its attached call to 165 seconds.
Generation itself still has its stricter 60-second encoder deadline. Docker
local logs must be `max-size=32768`, `max-file=1`, `compress=false`, verified
before start. Keep one new fixture at most 2,500,000 bytes, selected evidence
including executed snapshots at most 700,000 bytes per stage, and compact public
results at most 64,000 bytes per stage, within the existing project allowance.
Recheck storage and absence after each stage; preserve failures and do not retry
automatically. Q3 remains partial and Q4 remains not run until their other cases
have actual reviewed evidence.
