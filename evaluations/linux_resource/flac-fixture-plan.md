# Owned FLAC qualification preparation

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). The offline harness is
prepared; **no FLAC fixture, installed decoder image, native startup or decode
result is bound or claimed**. Existing CBR, VBR and overlap evidence remains
separate. Q3, Q4, native arm64 and release qualification remain incomplete.

## Execution gates

`owned_mp3_fixture.FLAC_ENCODER_REVIEW` and `owned_flac_decode.BINDING` are unset.
Both FLAC entrypoints reject before filesystem observation, allocation or fork.
Command-line execution flags cannot supply or override a review. A subsequent
reviewed source change must bind the actual evidence; synthetic unit-test values
are never execution configuration.

Before generation, review the retained FFmpeg binary's native FLAC encoder and
muxer route. Its earlier MP3 success does not establish availability or clearance
of this changed route. Before decoding, complete the separately owned metadata
recovery, linked ELF/initializer/loader inspection, actual compiled version-pointer
binding and authenticated single-package installation/image binding. Require the
final source, image, full installed ELF inventory, target and exact operator to
receive independent review. No download, build, installation or invocation is
performed by importing these modules or running ordinary tests.

The FLAC binding contains the reviewed image identity and review link, immutable
native argv/banner/ELF inventory, exact generated file size/hash, original PCM MD5
and metadata hash. The external operator must establish the actual source/image
and target; an image label in an in-container result is not that proof. No ELF
allowlist, encoded digest, original PCM MD5 or successful native observation is
invented in this change.

## One owned generation

Reuse the CBR generator's integer triangles unchanged: 5,760,000 stereo sample
frames, 48,000 Hz, signed little-endian 16-bit, exactly 120 seconds/23,040,000 bytes.
The original [generation receipt](results/2026-10-27-owned-mp3-fixture.yaml) records
pre-encoder SHA256
`0feb0dec8b6eb253893680342a58d1abb37727ef0da8f8aace849608ff41fa54`.
This is the exact lossless output expectation, not the lossy MP3 decode digest.
Compute SHA256 and MD5 incrementally in the one reusable 65,536-byte buffer;
retain partial-write offsets and accept the whole chunk before reusing it.
No full raw file is created. MD5 is the FLAC format checksum, not authentication.

The proposed fixed command reuses the retained encoder image
`sha256:f214f61096a8823a587859e4ce87616f25a63f77b37e659c79ef8757a6cef76c`
and FFmpeg ELF
`e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a`:

```text
/usr/bin/ffmpeg -nostdin -hide_banner -loglevel error -n
  -fflags +bitexact -f s16le -ar 48000 -ac 2 -i pipe:0
  -fflags +bitexact -map 0:a:0 -map_metadata -1 -c:a flac -threads:a 1
  -flags:a +bitexact -f flac /fixtures/owned-48000-stereo-120s.flac
```

The second `-fflags +bitexact` is an output option; the existing flag before `-i`
applies to input and `-flags:a` controls the codec. Pinned FFmpeg n7.1.5
[mux.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavformat/mux.c#L371)
otherwise adds encoder metadata, and
[flacenc.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavformat/flacenc.c#L63)
selects the vendor using the output muxer flag. Preserve both flags and the
unchanged MP3 recipes. This source finding does not complete execution review.

The output is a fresh seekable compressed file so STREAMINFO can be finalized.
Keep the existing generation owner: hard/soft FSIZE 2,500,000 bytes, CORE zero,
60-second work deadline, original three-second cleanup deadline, 65,536-byte
stderr cap and zero accepted diagnostics. Use the development-only 536,870,912-byte
group, swap zero, two CPUs, 64 tasks, nonroot UID/GID 1000, default seccomp, no
capabilities/new privileges/network, read-only root and one fresh writable
fixture directory. This is not the runtime-fit budget. Reconcile one compressed
artifact of at most 2.5 MB plus compact receipts with the existing allocation;
do not regenerate retained MP3 fixtures or silently increase caps/retry failures.

Require all raw bytes generated/accepted, the known raw SHA256, a nonzero raw MD5,
zero reaped exit, actual pipe/process release and bounded nonempty file size.
Record the whole compressed SHA256 and parsed metadata only after encoder cleanup.
STREAMINFO must have exactly 34 bytes and the expected rate/channel/bit-depth/frame
count; its nonzero MD5 must equal the generated raw MD5. Metadata is limited to
65,536 bytes and eight blocks, requiring exactly STREAMINFO, VORBIS_COMMENT
(vendor `ffmpeg`, zero user fields), and 8,192 zero padding bytes in that order.
Foreign APPLICATION, picture, seektable, cuesheet, ReplayGain/channel-mask tags,
duplicates and malformed lengths are rejected. An unexpected legitimate encoder
shape requires review; the parser does not widen itself.

## Native input admission and bounded output

The fixed decoder command is:

```text
/usr/bin/flac --decode --stdout --force-raw-format --endian=little
  --sign=signed --warnings-as-errors -- -
```

The selected stdin/stdout raw route neither resamples nor normalizes arbitrary
formats. The owned fixture supplies the required format. Do not pass encoding-only
`--channels`, `--bps`, `--sample-rate` or `--verify` to this decode command.

Reuse the existing owner with a small immutable fixed decoder binding. MPG123's
default argv, banner, mapping inventory and diagnostics remain unchanged. Fork
before parent PCM allocation, set hard/soft AS 12,524,288, DATA 12,000,000,
STACK 524,288 and CORE zero, and require unchanged literal `core` routing.
Retain the stdin writer without sending encoded input. Observe the complete
source-derived 238-byte native banner, then stop the actual child and verify its
PID/start identity, executable, single task, exact ELF mappings and full process
inventory, cgroup/security state and limits. Source ordering and exec-pipe closure
alone do not establish admission; the banner has no explicit Linux flush.
Actual compiled-version correspondence remains an execution gate.

Allocate both 2,304,000-byte PCM banks and the 4,194,304-byte copy arena only after
that gate. Authenticate exact compressed length/hash in its prepaid snapshot,
validate STREAMINFO/MD5 and exact metadata hash, recheck stopped ownership, and
only then resume input. The FLAC input partition ends at 2,500,000; the 65,536-byte
output view begins there, without overlap. No extra whole-payload copy is made
during decoding. Six-second readiness requires 1,152,000 aligned bytes within
15 seconds; the complete operation has the existing 150-second deadline.

Completion requires all authenticated input accepted and closed, actual stdout
and stderr EOF, zero reaped exit/no exec error, exactly 23,040,000 PCM bytes and
the original raw SHA256, valid diagnostics, no OOM/swap use, and actual release
within the original cleanup deadline. Count/hash equality participates in the
success predicate. The result records input completion and each EOF separately.
Failed cleanup retains descriptor/process debt; deadline failure is not release.

## Source-backed completion caveats

The pinned Debian [CLI main](https://sources.debian.org/src/flac/1.5.0%2Bds-2/src/flac/main.c/)
prints the banner before decoding. The reviewed
[decode path](https://sources.debian.org/src/flac/1.5.0%2Bds-2/src/flac/decode.c/)
uses stdout for raw output, enables MD5, rejects changed formats and most errors,
but does not check a final stdout flush. Its EPIPE branch can suppress error/MD5
reporting. Its sample-count check rejects fewer samples, not every excess-output
case. A `done` diagnostic or zero exit therefore never substitutes for EOF and
independent count/hash validation.

The foreign-metadata warning does not universally honor warnings-as-errors.
The parent accepts only the closed grammar derived from `decode.c`'s `print_stats`
and `verify_streaminfo`, with [utils.c](https://sources.debian.org/src/flac/1.5.0%2Bds-2/src/flac/utils.c/)
statistics formatting: exact banner, one `-:` prefix followed by a space, zero or more nondecreasing
decimal percentages 0 through 99 formatted `N% complete`, each followed by exactly
its byte count in backspaces, then one `done` plus nine spaces and LF. Source
suppresses 100-percent progress. Unknown-count text, extra warnings/errors and
arbitrary control erasure are rejected; total diagnostics remain at most 65,536
bytes. The banner reader tolerates fragmented delivery without consuming later
diagnostics. This grammar is a source-derived acceptance contract, not a native
observation from the pending installed image.

The native route is synchronous and owns ordinary mapped conversion, channel,
residual, metadata, bitreader, MD5 and stdio storage. The static conversion union
is not the whole native footprint: retained ELF inspection finds CLI BSS
2,884,720 bytes and page-rounded PT_LOAD spans 3,121,152 bytes. Charge all ordinary
native mappings under AS, including temporary missing-frame storage. Full linked
initializer/loader closure and actual startup/useful fit are still required.

Under the established closed-process/mapping premises the unchanged conditional
PCM bound is `2 * 12,524,288 + 8,802,304 = 33,850,880`, below 34,560,000.
The controlled-capacity total is `90,177,536 + 8,802,304 = 98,979,840`, strictly
below 100,000,000. Neither arithmetic nor offline tests prove Linux containment,
native fit, audio quality, Q4 or release readiness.

## Offline verification

`test_flac_contract.py` exercises malformed metadata, exact diagnostic grammar,
fragmented native admission, changed identity/mappings and absent execution
bindings. Injected syscall schedules exercise the actual shared pump/finally
block: partial I/O, wrong hash, short final output despite zero exit, excess PCM,
early exit, signals, failed readers, withheld EOF, rejected input metadata,
original cleanup deadlines and retained FD/PID debt. No native codec, fixture
generation or process/container launch participates in ordinary pytest.
Run locked sync, all hooks/strict Pyright and full pytest before pushing.
