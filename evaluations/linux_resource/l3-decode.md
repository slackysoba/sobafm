# L3 catalog decoder failures

**Candidate-image status: six cases unrun.** [L3](../../docs/linux-feasibility.md#later-application-qualification)
starts where `MediaProducer` gives a manifest's local `DecodeInput` and leased bank to
[`DecodeProducer`](../../src/sobafm/decode.py). Playback reads an exact size and SHA-256,
then closes stdin at verified EOF. Recording completeness and rights are checked at intake.
There is no HTTP fetch on this path. [Media manifest](../../src/sobafm/media.py) ·
[Source decision](../../docs/decisions/0007-fma-dataset-batch-catalog.md)

## Six cases and their evidence

The [one-case probe](l3_decode.py) calls the installed producer with its default decoder and
limits, except for the explicit fd-2 witness below. The first three cases use a regular file
of zeros against a 32-byte expected size. Their PCM limit is one 3,840-byte frame. Each
case keeps two checked marker frames in the other global PCM bank and reads one before and
one after the decode. This observes whether useful PCM survives in a separate lease.
The [adapter regression](../../tests/test_discord_adapter.py) `test_producer_failure_before_switch_keeps_heard_audio`
also checks that the current frame and heard identity survive a failed preparation.

| Case | Stimulus and required observation | Existing offline coverage in [test_decode.py](../../tests/test_decode.py) |
| --- | --- | --- |
| `truncated` | 31 bytes reach stdin. EOF on the local file yields `input_truncated`, 31 input bytes, incomplete transfer and invalid PCM. Bad input never earns stdin EOF. | `test_truncated_input_fails` |
| `corrupt` | 32 zeros match the manifest identity but contain no MPEG frame. Require complete transfer, 32 input bytes and zero output. A nonzero decoder exit yields `decoder_exit`. Exit zero yields decoder completion followed by `empty_pcm`. Both reject the stream. Record which actually happened. | `test_hash_mismatch_never_gives_the_decoder_eof` separately covers same-size damage **after** admission. `test_nonzero_exit_after_full_output_fails` and `test_output_under_one_frame_fails` cover output judgment. |
| `oversized` | The first read returns 33 bytes, one over the declared size. The producer rejects the entire chunk before writing any of it. Require `input_oversized`, zero input bytes, incomplete transfer and invalid PCM. | `test_oversized_input_fails`; `test_output_overrun_fails` covers the separate PCM ceiling. |
| `feed` | A local FIFO withholds EOF after one byte. Wait until the real feeder reports that byte written. Cancel, then close the evaluation writer to let the original read settle. Require the one-byte witness, incomplete transfer and an owner cancellation. | `test_owner_cancelling_the_bank_first_keeps_its_clock_and_is_no_failure` covers blocked stdin writes. `test_held_close_keeps_owner_and_lease_and_expiry_stays_failed` covers late I/O release. The FIFO injects a blocked source read. A stalled disk or a network service has different recovery behavior. |
| `reader` | Withhold bank reads while the real decoder consumes the approved owned CBR fixture. Wait for exactly 2,304,000 buffered bytes, then cancel. Require the full unread bank witness, readiness, 2,304,000 written bytes, no emitted frame and an owner cancellation. Record the actual transfer state. | `test_full_bank_waits_for_the_consumer` resumes a consumer. `test_cancel_unblocks_a_writer_that_defers_term_and_exit_zero_is_not_complete` covers cancellation. A full bank witnesses application backpressure; a syscall-level blocked decoder write was observed separately in Q4. |
| `stderr` | Playback supplies `DEVNULL`, so there is no diagnostic reader or queue to flood. A shell stand-in verifies its fd 2 is the null device, writes 64 × 4,096 diagnostic bytes, then emits one recognizable frame on stdout. Require the frame, verified short readiness, complete transfer, decoder completion, valid PCM and audible drain. | `test_complete_decode_reports_every_fact` covers ordinary completion. The shell tests the actual launcher's redirection under the default limits. Stock mpg123's diagnostic content and volume remain unmeasured. |

For the two size errors the feeder can terminate the child during launch. Record its
actual return code and decoder-complete flag independently of transfer. Require zero PCM
for these tiny inputs. In both cancellation cases expect `failure=null`, a reaped child,
`decoder_complete=false` even for exit zero, and invalid PCM. All five rejected or
cancelled streams must end without audible drain. An unexpected outcome, including
usable PCM from the corrupt fixture, is a failure to investigate.

[Prior Q3/Q4 results](../../docs/linux-feasibility.md#recorded-native-amd64-decoder-proof)
bind CBR completion, cancellation during feed and stdout backpressure to their own images,
targets and earlier harness. The [production launch check](https://github.com/slackysoba/sobafm/pull/143#issuecomment-6681000575)
observed full-bank cancellation and release on its image. Its delayed-`finish` runs expired.
Start the one `finish()` owner immediately, as the service does. The ordinary locked suite
also covers caller-owned lease release in [test_app.py](../../tests/test_app.py).
To exercise the six probe schedules in the locked Linux development checkout, run
`uv run pytest -q evaluations/linux_resource/test_l3_decode.py`. These checks use isolated
no-media children and verify the witnesses, original deadline, terminal frames and refunds.

## One separately authorized target run

1. Record the reviewed probe commit, full application source SHA, local image ID by digest
   from its build record, dependency manifest (including mpg123 and its closure), native
   amd64 Docker target, kernel and page size. Verify the selected local image, source-to-image
   mapping and target before starting. Use the probe extracted from the reviewed commit.
2. Use only the probe-generated zeros, markers and FIFO. For `reader` only, obtain specific
   approval for the **existing owned synthetic** 1,920,768-byte CBR fixture. Check its size
   and digest against the [Q3 confirmation](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/results/2026-10-28-owned-mp3-decode-confirmation.yaml)
   **before** mounting that file read-only. Cancellation precedes final hash validation.
   If unavailable, record `reader: not-run`. Do not fetch, regenerate or substitute media.
3. Run one case at a time in a fresh isolated container. Set `IMAGE` to the immutable
   local image ID, `PROBE` to the absolute extracted script path, `CASE` to one table
   name and `RUN` to a fresh private identifier. Reserve the name before creating anything.
   In Bash, set `mounts=()`. For the approved reader case only, set
   `mounts=(--mount "type=bind,src=$FIXTURE,dst=/fixture/owned-cbr.mp3,readonly")`.
   Use a fresh private output directory and capture the command's own exit status.

```bash
name="sobafm-l3-$RUN-$CASE"
(
  set -o noclobber
  ulimit -f 32
  timeout --kill-after=1s 24s docker run --pull never --name "$name" \
    --label "org.sobafm.l3-run=$RUN" --init --platform linux/amd64 --network none \
    --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
    --memory 90177536 --memory-swap 90177536 --pids-limit 64 --cpus 2 \
    --restart no --log-driver none --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64k,mode=1777 \
    --mount "type=bind,src=$PROBE,dst=/l3.py,readonly" "${mounts[@]}" \
    --entrypoint /app/.venv/bin/python "$IMAGE" -I /l3.py "$CASE" > "$CASE.jsonl"
)
```

The probe applies the crash-dump controls and installed limit gate. The whole-service
group has 90,177,536 bytes, zero swap, 64 tasks and two CPUs. The two 2,304,000-byte
banks and the 4,194,304-byte copy reservation bring controlled capacity to **98,979,840
bytes**. There is one child per case (two native spaces maximum including debt),
with CORE 0, STACK 524,288, DATA 12,000,000 and AS 12,524,288. The feeder uses 16,384
bytes, stdout reads directly into one bank region, the output overrun probe uses one
byte, and the observer compares one frame at a time. Charge these to their existing
owners. The aggregate native PCM bound remains 33,850,880 bytes under the reviewed closure.
[Capacity partitions](../../docs/linux-feasibility.md#capacity-ownership-and-admission)

## Reading and closing a case

- Require `witness_seconds <= 15` for each stall. For the four other cases require
  `cancelled_at=null` and cleanup started within 15 seconds of start. A watchdog cancellation
  cannot pass for a natural failure or the intended stall. There is one terminal `finish`
  report. Compare transfer, input count, decoder completion/return code, readiness, valid PCM,
  written/buffered bytes and final audible drain against that case's row. Require
  `old_frames_preserved` and `end_of_stream`. Only the stderr case returns the diagnostic frame.
- Require `cleanup=clean`, `settled=true`, a non-null original cleanup start and
  `cleanup_deadline = cleanup_started_at + 3.0`. For the stalls it starts at the first
  cancellation. The final monotonic `released_at` must fall within that same deadline.
  `decode.release()` observes reaping, joined I/O and closed descriptors. The caller then
  refunds its bank. Require the old-bank refund and zero leases. A later release never
  changes an expired verdict. Missing release evidence leaves debt outstanding.
- Retain the group peak and events, swap, task counts, command exit status and exact-container
  exit/OOM state. Require no limit event, OOM or swap, the group peak inside its cap,
  tasks back to the initial level, complete rows and exit zero. Compare the case
  predicates manually. Exit zero alone means the observations were collected.
- Allow 25 seconds for the invocation and 15 more to inspect, stop if needed, remove and
  verify absence: **40 seconds per case, four minutes for six**, plus one minute for preflight.
  Bound cleanup CLI waits by the remaining time. A CLI timeout may leave the container
  running. Reconcile only the reserved name, matching run label and actual ID. Record state
  before removal. Stop the sequence on a failed case, missing row or uncertain release.
  Keep its failed verdict, unconfirmed owners, identity and recovery action until reconciled.
- Retain at most 32 KiB per case plus a 4 KiB binding/summary (196 KiB total). Keep one row
  per case with pass/fail/not-run and the evidence above. No audio, raw diagnostics,
  environment, private paths or service credentials belong in the record. No execution of
  L3 establishes station transitions, listening quality, Discord behavior or release readiness.
