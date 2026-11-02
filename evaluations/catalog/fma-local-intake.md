# Bounded local FMA intake preparation

Issues [#40](https://github.com/slackysoba/sobafm/issues/40) and
[#42](https://github.com/slackysoba/sobafm/issues/42) implement an explicitly invoked
Linux intake and one owned validation reader for **Night Owl by Broke For Free**.
Development and regression tests use synthetic bytes only. The maintainer supplied
an original on 2026-11-02: 8,054,458 bytes, SHA-256
`7952dce61b28f454ff3ee0183f85df52b2a9f2edfcd6a0ebafc12f981f666ed5`.
Read-only identity preparation did not establish format, complete-file integrity
or embedded notices. Keep that original alone until a bounded inspection consumer
and its exact execution scope are reviewed. No actual copy, parsing, decode,
playback, admission review or provider request is released here.

## Operator acquisition boundary

An operator may propose one file obtained through FMA's ordinary permitted web
workflow. Before a real run, supply its exact local file, canonical recording URL,
versioned acquisition evidence, requested absolute expiry, current metadata/credit
snapshot and complete download-time notices for a concrete scope review. Do not
infer acquisition time from filesystem timestamps. Keep credentials, cookies,
private filesystem locations and the original recording outside Git and public
issue evidence. No credentials are accepted by this module.

The accepted [HEAD observation](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6552729791)
and subsequent [single GET observation](https://github.com/slackysoba/sobafm/issues/1#issuecomment-6556442342)
both redirected to FMA login. The GET at **2026-11-01 09:56:51.809867 UTC** returned
HTTP 302, `text/html`, with Location `https://freemusicarchive.org/login`. No body
API was called and no plaintext body bytes were delivered; this does not claim
zero kernel or TLS buffering. No redirect, retry, authentication or alternate
route followed. The child exited and its reader/pipes settled with no remaining
debt. No cleanup clock started, so that observation establishes no three-second
cleanup bound. Anonymous audio access remains unestablished. This intake performs
no network operation and cannot bypass that boundary.

The [catalog snapshot](fma-night-owl-catalog.json) remains unchanged and unadmitted.
Its current parsed recording digest is
`741591d22c948f3caf790285d74ac4d855d5732b76463babe41a8ab73e1198bb`, and its credit
snapshot is
`fma-aba6502c18a4540d8394516aca393cbba8dda7c4dfa417f97aa379e0e366d483`.
The new GET observation does not rewrite the dated metadata evidence.

## One owned destination

Configure one empty, project-owned, mode-0700 directory on the Linux filesystem.
It must have an absolute path with no symlink components, and must differ from
the operator original's directory. All intake/recovery attempts for this proposed
copy use that same slot. Do not create another slot to evade a retained copy or
cleanup debt. Do not move, edit, hardlink, replace or externally remove its files
while the owner is active. Exclusive ownership of this private directory is a
precondition; the advisory lock coordinates cooperating intake processes, not
hostile processes running as the same user or root.

The slot contains `owner.lock`, a bounded `state.json` ownership record and at most
one `payload`. During atomic state replacement it may also contain `state.tmp`;
each state record is at most 16,384 bytes. Only `payload` holds candidate bytes,
including a partial or quarantined copy, with a hard **20,000,000-byte** ceiling.
Copy attempts never create another candidate or resume an interrupted one. The
operator original is read-only and is never deleted. A filename or extension is
not evidence of format, recording correspondence or complete-file integrity.

Prepare a UTF-8 JSON manifest with exactly these fields. The placeholders below
must be replaced with reviewed values; this example is deliberately not runnable.
Obtain file identity and SHA-256 from the exact operator-supplied regular file.
Device, inode, bytes and nanosecond timestamps are JSON integers. Links, special
files, zero bytes and over-limit inputs are rejected.

```json
{
  "schema": "sobafm-fma-intake-2",
  "source_url": "https://freemusicarchive.org/music/Broke_For_Free/Directionless_EP/Broke_For_Free_-_Directionless_EP_-_01_Night_Owl/",
  "metadata_digest": "<current parsed recording digest listed above>",
  "credit_snapshot": "fma-aba6502c18a4540d8394516aca393cbba8dda7c4dfa417f97aa379e0e366d483",
  "acquisition": {
    "schema": "sobafm-acquisition-1",
    "retention_anchor_utc": "<reviewed exact instant or conservative lower bound, UTC Z>",
    "basis": "conservative_lower_bound",
    "operator_evidence": "<bounded operator report and explanation of the chosen bound>"
  },
  "expires_at": "<fixed absolute UTC expiry, at most seven days after the anchor>",
  "download_notices": ["<complete actual download-time notice>"],
  "file": {
    "device": "<integer>",
    "inode": "<positive integer>",
    "bytes": "<integer from 1 through 20000000>",
    "mtime_ns": "<integer>",
    "ctime_ns": "<integer>",
    "sha256": "<64 lowercase hexadecimal characters>"
  }
}
```

Notices must be 1–16 nonempty, trimmed strings, each at most 1,024 characters,
with no control characters; their encoded aggregate is at most 4,096 bytes.
Preserve their meaning in that bounded representation. Oversize or ambiguous
notices require a scope decision rather than silent truncation. The whole
manifest is limited to 16,384 bytes; duplicate or unknown fields fail validation.
The current catalog must independently match the fixed recording digest and
credit snapshot. A changed snapshot requires reviewed code/evidence changes.

The acquisition record is closed and versioned. `basis` is either `exact` or
`conservative_lower_bound`; operator evidence is a nonempty, trimmed, control-free
string of at most 1,024 characters and 4,096 encoded bytes. The anchor, basis and
evidence all bind the request fingerprint and durable ownership record. Safe
receipts retain the anchor/basis and evidence hash, not the raw evidence text.
An exact anchor claims an actual instant. A conservative anchor is explicitly an
earlier retention bound, never a fabricated precise acquisition timestamp.

For the supplied original, the maintainer reports downloading just before sending
the path on **2026-11-02 America/Los_Angeles**. Exact seconds are unknown. The
conservative lower bound **2026-11-02T00:00:00Z** permits expiry no later than
**2026-11-09T00:00:00Z**. Retain that report and the lower-bound explanation in
`operator_evidence`; prefer a much shorter fixed execution window. No filesystem
timestamp supplies acquisition evidence. The reported download notice says the
artist should be attributed: preserve this as an operator-reported paraphrase,
not literal independently captured FMA text. Complete credits through `/now`
remain required; bot status is best effort and cannot substitute for them.

Historical `sobafm-fma-intake-1` manifests keep their original `acquired_at` exact
instant semantics, original fingerprint and v1 state/receipt shape. They encode
no operator acquisition evidence and cannot obtain a reader handoff. Existing v1
intake and cleanup-only recovery remain supported; v1 is never silently converted
to a conservative estimate. New reader work requires v2. Cross-version or changed
anchor/basis/evidence recovery fails closed with unresolved debt.

After an actual file and scope have been released, the operator invokes:

```sh
uv run --locked python -m sobafm.fma_intake \
  --execute-reviewed-local-intake \
  --source /absolute/operator-supplied-file \
  --directory /absolute/reviewed-project-pending-slot \
  --manifest /absolute/reviewed-intake-manifest.json \
  --catalog evaluations/catalog/fma-night-owl-catalog.json
```

The command first validates the request and reads/hashes the exact supplied file,
checking its identity before and after each bounded read. Only then does it claim
the destination. A second bounded pass copies and hashes the bytes, checks source
mutation/substitution, checks exact count/hash and synchronizes the copy. Failure
cannot publish a valid pending receipt. There is no format probe, decoder, audio
analysis, provider adapter, SDK, service or new dependency.

## Pending lifetime, cancellation and recovery

The command **stays alive** as the pending copy's expiry and cleanup owner. A
pending receipt does not end its responsibility. Keep the process and original
manifest available until cleanup settles. The expiry is absolute, strictly after
the retention anchor and no more than seven days later. An in-process monotonic deadline
also prevents a wall-clock rollback from extending it. Copying, retrying and
restarting cannot renew it. An expiry detected during copying or state persistence
cannot produce a valid pending receipt.

Ctrl-C or SIGTERM requests deletion of the exact owned destination. Expiry does
the same. The first cancellation/expiry starts one three-second cleanup deadline;
repeat requests cannot restart it. A blocked read/write/close or failed deletion
keeps its original thread, file descriptors and capacity debt until actual
settlement. Closing the files is insufficient: a caller must observe the worker's
actual termination before successful cleanup timing can be reported. The release
timestamp is that external observation; a late first observation conservatively
fails timing even if the worker may have exited sooner. Passing the deadline
permanently fails that timing verdict even if
later cleanup succeeds. The owner retries settlement of the same resources;
it never retries acquisition. Do not kill it merely because cleanup is late.

An unexpected process/host exit prevents unattended expiry enforcement. Treat the
slot as retained debt until reconciliation; do not claim expiry deleted it or
allocate another copy. Recovery uses the same manifest and exact directory:

```sh
uv run --locked python -m sobafm.fma_intake \
  --execute-reviewed-local-intake --recover \
  --directory /absolute/reviewed-project-pending-slot \
  --manifest /absolute/original-reviewed-intake-manifest.json \
  --catalog evaluations/catalog/fma-night-owl-catalog.json
```

Recovery verifies the owner identifier, original manifest fingerprint and times,
directory and lock identities, and payload identity. It never reads the original,
recopies bytes or approves a pending copy. An interrupted owner cannot prove its
old cleanup timing; recovery preserves any recorded cleanup start and reports a
failed timing verdict. If no cleanup start was persisted, the old deadline is
explicitly unknown (`null`), with no fresh recovery allowance. Missing,
invalid, unbound or substituted ownership evidence starts and stays in this
unknown/failed state; closing the current attempt's handles cannot establish the
old slot's timing. Inconsistent ownership evidence
leaves debt and blocks reuse. An uncertain temporary state or moved payload needs
scoped reconciliation; no path-only deletion or blanket cleanup is attempted.

After actual release, keep the bounded receipt and ownership record as evidence.
The slot remains single-use; the implementation does not automatically retire
records or start another intake. Any later retirement or new allocation requires
the applicable scoped authorization and storage accounting.

## One owned seek/stream consumer

From a live validated v2 pending owner, `owner.read_into(consumer)` atomically
admits one initially inert `ReadConsumer` and returns an `OwnedRead` handle. A
second handoff is rejected. The caller retains responsibility for construction
failure or rejected admission: construction must not already launch a child or
acquire backing. The owner persists `reader_state: owned` before opening the
reader or calling the consumer. Registration is revoked if cancellation wins
before start; the inert consumer is then released without invoking it.

The consumer implements five small operations:

| Operation | Ownership contract |
| --- | --- |
| `inspect(fd)` | Synchronously inspect the borrowed, registered `O_RDONLY` descriptor using maintained seekable file facilities. Do not close or replace it. Return only after all inspection reads/seeks, inherited/duplicated handles and helper operations actually settle. Raise on failed, late or uncertain inspection. |
| `write(chunk)` | Accept one stream chunk of at most 65,536 bytes. Consume it synchronously, or retain it only within an explicit prepaid consumer backing limit; do not accumulate a whole-file snapshot. |
| `finish()` | End input and signal downstream completion. Returning does not certify child exit, file completeness, credits or admission. |
| `cancel()` | Nonblocking, idempotent cancellation of every downstream operation. It may run concurrently with the other callbacks. |
| `join(timeout)` | Wait at most the supplied timeout. Return `True` only after all downstream processes, readers, writes, descriptors and retained chunk/backing references actually settle. |

Callbacks run outside the owner lock. Only operation admission/registration uses
the lock: cancellation revokes new admissions but does not pretend an already
admitted blocking call ended. The owner retains both the reader and cancellation
worker if a consumer violates the nonblocking/timed contract. A held read, inspect,
write, finish, cancel, close or join remains cleanup debt under the original
deadline; there is no additional reader grace period or capacity-free verdict.

The descriptor always refers to the same pending inode. After inspection, the
owner checks its access mode, identity/size/timestamps and the named payload, then
rewinds it for bounded incremental delivery. It checks identity around each read
and verifies the entire streamed count and SHA-256 against the pending copy before
calling `finish`. A truncated, mutated or substituted file cannot complete the
handoff. A consumer that closes/replaces the borrowed descriptor causes uncertain
descriptor debt, not permission to close an unrelated replacement.

Inspection's extra reads/seeks are **not** bounded or counted by the raw descriptor
itself. The concrete inspection consumer must enforce and report its cumulative
read, seek, metadata-allocation and time limits. Stream counters cover the later
incremental pass only. Data and downstream results remain provisional until
`reader.join(timeout)` observes actual settlement and its safe report says
`completed: true`. Even that result establishes byte-stream integrity only.
The read handle's timeout includes waiting for cancellation-worker startup and
the final ownership check. A pending startup returns `False` when that wait
expires; it cannot produce a released verdict or a new cleanup deadline. Worker
joins run outside the owner lock against a stable, fully started target.

A maintained `subprocess.Popen(..., pass_fds=(fd,))` without `preexec_fn` can pass
the descriptor to an owned **fresh local Linux helper**. That helper can inspect
seekably; a future fresh single-threaded native-proof runner can fork its decoder
before starting input/read threads. Do not fork native proof code from the live
multithreaded intake process. The synthetic tests exercise local seekable helper
settlement only. Inheriting a host descriptor does not carry it across Docker's
daemon/container boundary. A later container topology needs independently reviewed
same-inode access and actual container settlement; no extra payload, untracked
path opening, procfd workaround or generic container/IPC machinery is supplied here.

The controller polls the read handle and owner, requests `owner.cancel()` on
failure, expiry or completion of the finite evaluation, and retains both until
actual join/release. The owner deletes the payload only after its registered
reader, downstream operations and descriptors settle. Persisted `reader_state:
owned` after interruption means uncertain external ownership: cleanup-only
recovery refuses to delete/reuse that slot. It does not assume process exit closed
a separately launched child or its handles. A settled reader is durably recorded
as `released`; the one-use slot and original expiry remain unchanged.

## What the receipt establishes

Receipts are bounded and contain no source/destination paths or raw notices. They
bind the file byte count/hash, canonical source, metadata/credit snapshot, notice
count/hash, versioned acquisition evidence/expiry, owner, retained work and actual release
or cleanup debt. Keep the original bounded manifest for notice review. The durable
state is an ownership ledger; the final process receipt additionally accounts for
descriptor closure and the worker's actual exit. A `pending_valid` observation
is time-specific and expires at the original deadline.

The copy is **pending review**. Complete-file and embedded-notice inspection,
recording correspondence, conflicts in credit evidence and an exact trusted
admission review remain separate. No availability, access-ready or complete-credit
claim is promoted. The reported **194 seconds of audio** and the earlier decoder
case's **150-second operation wall-clock deadline** are different quantities;
194 seconds alone establishes neither fit nor incompatibility. The existing
case's PCM limits and all source/image/target-bound proof records remain unchanged.
A real-file evaluation must review its own complete-input/output bounds without
truncating the recording or implicitly expanding the earlier case. No native
decoder execution is permitted here.

The deterministic tests use small synthetic files, explicit read/deletion barriers
and an interrupted synthetic child process. They exercise partial copy/write,
mutation/substitution, repeated cancellation, mid-copy and pending expiry,
wall-clock rollback, failed close/deletion, failed state sync, restart binding and
original preservation. They establish local ownership behavior only, not music
quality, recording validity, Linux containment or release readiness.

The concrete [#44 validation consumer](../fma_validation/README.md) uses this
contract with a separately contained metadata helper, then a fresh streaming
decoder runner. A trusted enclosing evaluation may convey an earlier UTC cleanup
start to `LocalIntake.cancel(started_at=...)`; it can only tighten the original
deadline, never repair unknown recovery timing. Successful read completion must
be observed before that cancellation request. Actual native/source execution
requires the separately reviewed finite packet described there.
