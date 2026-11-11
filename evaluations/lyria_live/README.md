# First free-tier Lyria check

Preparation for [#165](https://github.com/slackysoba/sobafm/issues/165), corrected
under [#182](https://github.com/slackysoba/sobafm/issues/182). The
[first authorized attempt failed without an evaluator record](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6794025268).
Provider contact and key acceptance remain unknown; its exact exception was not
retained, so the historical cause is unproven. That one-attempt permission is
consumed. **A fresh one-attempt authorization is required after review and merge
before any further live invocation.** Default invocation prints the proposed
bounds and reads no key. The opt-in evaluator is outside ordinary tests and CI.

```sh
uv run python evaluations/lyria_live/probe.py
uv run pytest -q evaluations/lyria_live
uv run pyright evaluations/lyria_live
```

## Smallest useful scope

Use the installed production `LyriaSession`, with its actual setup, prompt,
configuration, PLAY, message validation, receive limits and cleanup. This avoids
`GeneratedStream`'s automatic retries. One session is enough to answer first
availability; renewal, Gemini interpretation and Discord are separate scopes.
The pinned HTTP client can also retry a disconnected WebSocket upgrade internally.
For this isolated evaluator process, a guard around the production client's
existing socket factory permits only its first allocation and refuses all later
allocations before connection, including address fallback and handshake retry.
The first socket still uses the production receive limits and transport path.
The guard remains installed through actual settlement, then restores the factory.
This intentionally stricter connection budget is specific to this check; it does
not change the installed application or claim to test its retry/fallback behavior.
One socket alone is not a general HTTP request bound. Here the production client
uses a fresh `force_close=True` connector and rejects redirects in its middleware.
The pinned aiohttp send/response-start failure path closes its connection before
its internal retry; it cannot resend that upgrade on the existing connection.
Together these paths and the allocation guard permit at most one HTTP upgrade.
Raw-peer regressions count both connections and upgrade requests for a dropped
handshake and a redirect that offers to keep the connection open. A connection
failure or refused address fallback is inconclusive about account/model access;
it is not evidence that free-tier Lyria is unavailable.
The fixed non-personal prompt is “gentle instrumental jazz, piano and soft drums”,
at 90 BPM. Nothing is played, uploaded as audio, saved or listened to.

| Bound | Maximum |
| --- | --- |
| Connections / requests | One WebSocket upgrade; setup, prompts, configuration and PLAY (four JSON messages); zero retry, renewal, paid fallback or alternate endpoint/model |
| Work | 25 seconds, including connection and preparation; production readiness retains its own 15-second limit |
| Consumption | 200 frames, one per 20 ms, discarded immediately: four seconds / 768,000 bytes |
| Decoded PCM | 3,072,000 bytes (16 seconds): the 2,304,000-byte bank plus the maximum consumption; the producer cannot commit beyond available bank space |
| Backpressure | Wait until less than one maximum message fits, hold one second without consumption, require unchanged buffered bytes, then require PCM production resumes as frames are consumed |
| Cleanup | Original production three-second deadline; expired verdict stays failed; keep joining before releasing the bank, with no later admission |
| Process stop | 35 seconds after entering live main; hard stop is exit 124 and settlement unconfirmed, never a cleanup pass |
| Output | One short JSON report, or a hard-stop record; no audio, headers, provider payload, exception text, credential-bearing URL, prompt or key in output; retain at most 64 KiB of results and one 64 KiB uncompressed container log |

The PCM bound concerns decoded/admitted audio, not how much the remote service
generates or unread network traffic. Production receive staging remains in the
unchanged copy partitions. Control messages are size-bounded by production and
time-bounded by this run; this is not a total-network-byte meter. The shared
90,177,536 + 4,608,000 + 4,194,304 = 98,979,840-byte contract is unchanged.

## Bind the candidate before asking to run

The existing retained image is
`sha256:bd7d1aee495e612a8f7a168b767ce43eb996466d75e743e0f7997807fbb7fe23`,
with application source `01e2226adeeb49f676b525b9e903385f4960a1fd`.
It is **not** main `37e2b8fbe5bef54f2884240571516779faf7eee0`.
The reviewed comparison of `lyria.py`, `pcm.py`, `crash_dumps.py` and `uv.lock`
finds only subsequent Lyria failure-category changes. Its handshake, bounded
receive, PCM and cleanup code are unchanged. Its 429 category is `connect`,
401/403 is `rejected`, and a WebSocket 1011 is `closed`; this evaluator stops on
every failure with zero retries. Do not infer an exact HTTP status from these
older categories. A new image is unnecessary for this narrowly named old-source
availability check. A result cannot qualify current main's new error categories.

Reuse the corrected, independently reviewed synthetic
[L2 evidence](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6789773813)
for its stated source/image/target only. It does not establish provider access.

Before execution, record privately the reviewed evaluator commit and its
`probe.py` SHA-256, the image ID, actual application source above, Linux amd64
target, kernel and page size. Use the image's installed package, with only
`probe.py` mounted read-only at `/eval/probe.py`; never mount a checkout over its
application. Verify the image ID/OS/architecture using an explicitly formatted
image inspection, and do not pull or build another image implicitly. The result
prints kernel, architecture and page size. Keep source/image/harness bindings
with it in the final evidence comment.

## Proposed operator walkthrough

1. The coordinator obtains separate explicit approval using the question below,
   after independent review and merge. The operator confirms in their account UI
   that the chosen project has no billing enabled and is using free-tier access.
   `--confirm-free-tier` records the operator's choice; it cannot verify billing.
   Stop if that account fact is unknown. A successful Gemini request is irrelevant
   to whether Lyria is available. Do not enable billing or try another key/project
   after a refusal.
2. The coordinator names one new private plaintext key file in chat (outside the
   repository). The operator opens that exact file in Notepad, pastes only the
   Lyria API key, saves and closes it. Do not use either existing real environment
   file or paste the key into chat, a terminal command or command arguments.
   The agent may check file existence/size, never contents. No copying or shell
   sourcing of its contents. Mount that file read-only as `/run/lyria-key`; only
   the evaluator process opens it, after dump controls and resource gates.
3. Prepare one uniquely named container from the exact retained image: no restart,
   UID 1000, init, read-only root, default seccomp, all capabilities dropped,
   no-new-privileges, core ulimit zero, 90,177,536-byte memory and memory-plus-swap,
   64 tasks and two CPUs. No catalog, data volume, Docker socket or Discord token.
   Mount only the reviewed script and private key. Use the local log driver with
   `max-size=64k`, `max-file=1`, `compress=false`. Ordinary provider egress is
   required; do not claim the default Docker network filters destinations.
4. The prepared `run.sh` implements these Docker operations. Invoke it from Linux
   with the reviewed script beside it and the private file's absolute path:

   ```sh
   timeout --signal=TERM --kill-after=5s 70s bash evaluations/lyria_live/run.sh \
       --send-to-lyria --confirm-free-tier /absolute/private/lyria-key.txt
   ```

   This overrides the entrypoint with the installed interpreter and runs
   `/app/.venv/bin/python -I /eval/probe.py --send-to-lyria --confirm-free-tier`
   once. The image installs the application into that environment; no
   `PYTHONPATH`, source-path bootstrap or application mount is needed
   ([packaging review](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6794098907)). The script
   has no endpoint or bound override. Run it under an external 40-second deadline
   as well as its own 35-second watchdog. Give each Docker setup/inspection command
   a two-second timeout with one further second to kill a stuck CLI. Allow
   20 seconds for setup and result inspection,
   40 seconds for the attached run and 15 seconds for cleanup: **75 seconds total**.
   The outer watchdog signals at 70 seconds and kills at 75; any interruption
   fails the run. Use the exact `timeout` invocation above; a client
   command timeout does not prove the daemon stopped the container.
5. At completion or any timeout, inspect only this container's exact ID, image,
   ownership label, running/exit/OOM state. Stop it immediately if still running,
   then remove only this owned evaluation container and verify absence, within
   the 15-second cleanup allowance (five bounded operations). Never force-remove
   an unidentified container. A missing create response, daemon timeout,
   or unconfirmed absence leaves recovery debt; retain the name/label and
   report incomplete. A stop/removal command failure also fails the attempt, even
   if later absence is verified: `cleanup_commands_ok=false` preserves that failed
   operation while `cleanup_verified=true` records the observed release. In that
   case no container recovery debt remains. The wrapper stores its recovery name/label before create
   under a private `sobafm-lyria.*` temporary directory; keep that small record
   if recovery is needed. Only its final `wrapper=pass` row, exit zero and a
   successful outer watchdog exit complete the check. No final row means failure.
   Do not retry the provider. Keep the operator's key file;
   its later removal is the operator's decision.

This is a reviewed execution procedure, not permission to run these steps now.
The wrapper's host watchdog bounds the client commands, not the Docker daemon.
It never treats a timed-out CLI as proof of cleanup and never deletes recovery
records or the key. There is no provider call on default `bash run.sh` invocation.

## Verdict and remaining limits

Pass requires exit zero, `result=pass`, first PCM and six-second readiness,
unchanged held bank contents, 200 valid frames, resumed production, PCM within
the bound, clean original-clock cleanup, release within three seconds and zero
leased banks, plus observed container exit without OOM and verified removal.
Any missing record or failed observation fails the check. Preserve the failure
even if later cleanup succeeds. Provider refusal or quota ends this attempt and
returns the decision to the maintainer; it never triggers spending or retries.

Startup reports use only fixed reasons: `startup_import` for application or
dependency imports, `crash_controls` for applying the process controls, and
`startup_internal` for module initialization. Crash controls still precede native
dependency imports and key access. Later preflight failures distinguish `target`,
`limits` (including unreadable or malformed cgroup values), `credential_file`
(read, encoding or validation failure), and `internal`. None includes exception
text, a traceback or provider diagnostics. These reasons locate a failed stage;
they do not establish provider contact or explain the first attempt retrospectively.

The wrapper distinguishes `log_retrieval` (failed or timed-out Docker log command),
`log_size` (output over 64 KiB), `evaluator_output_missing` (successful collection
without a result record, including stderr-only startup output), and `client_result`
(a collected evaluator refusal or nonzero client exit). Container stderr remains
discarded, never saved or forwarded, even when collection fails. A partial log
from a failed collection cannot pass. These failures still run the same owned
container cleanup and preserve its independent verification and failed-command
flags. Offline tests use synthetic imports, keys and Docker command doubles plus
loopback transport peers; they make no live availability or image-execution claim.

This checks availability at that moment on the operator-confirmed free-tier
project and short production transport behavior. It does not establish ongoing
quota, sustained real-time throughput, two/three-server capacity, music quality,
wordless vocals, natural session lifetime, renewal, fades, station commands,
Discord, all memory partitions or release readiness. No audio is available for
listening afterward. Under the recorded free-tier decision, prompts/output may
be used by Google with human review; eligibility and audience limits remain in
[provider access](../../docs/provider-access.md). Recheck its linked official
sources before execution; this preparation makes no new claim about current
provider terms or published access.

## Exact approval question for the coordinator

“May I run one free-tier Lyria RealTime session using the reviewed evaluator and
the retained image/source named above, without rebuilding, with your privately supplied key for a
project you confirm has no billing enabled? It sends the fixed instrumental
prompt, consumes and discards at most four seconds of audio, admits at most
16 seconds of decoded PCM, makes no retry or renewal, stops work by 25 seconds,
hard-stops the evaluator by 35 seconds, and limits the whole attempt including
container cleanup to 75 seconds. There will be no Gemini interpretation, Discord,
listening, audio capture, paid fallback or billing change. Refusal, quota or any
unconfirmed cleanup ends this attempt and returns the decision to you.”
