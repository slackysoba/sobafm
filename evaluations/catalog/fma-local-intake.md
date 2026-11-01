# Bounded local FMA intake preparation

Issue [#40](https://github.com/slackysoba/sobafm/issues/40) implements an offline,
explicitly invoked Linux intake for the nominated **Night Owl by Broke For Free**
recording. Development and regression tests use synthetic bytes only. No actual
recording file has been supplied or acquired, and this implementation does not
release an actual intake, decode, playback, admission review or provider request.

## Operator acquisition boundary

An operator may propose one file obtained through FMA's ordinary permitted web
workflow. Before a real run, supply its exact local file, canonical recording URL,
actual acquisition UTC time, requested absolute expiry, current metadata/credit
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
  "schema": "sobafm-fma-intake-1",
  "source_url": "https://freemusicarchive.org/music/Broke_For_Free/Directionless_EP/Broke_For_Free_-_Directionless_EP_-_01_Night_Owl/",
  "metadata_digest": "<current parsed recording digest listed above>",
  "credit_snapshot": "fma-aba6502c18a4540d8394516aca393cbba8dda7c4dfa417f97aa379e0e366d483",
  "acquired_at": "<actual acquisition in YYYY-MM-DDTHH:MM:SS[.ffffff]Z>",
  "expires_at": "<reviewed absolute UTC expiry, at most seven days after acquisition>",
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
acquisition and no more than seven days later. An in-process monotonic deadline
also prevents a wall-clock rollback from extending it. Copying, retrying and
restarting cannot renew it. An expiry detected during copying or state persistence
cannot produce a valid pending receipt.

Ctrl-C or SIGTERM requests deletion of the exact owned destination. Expiry does
the same. The first cancellation/expiry starts one three-second cleanup deadline;
repeat requests cannot restart it. A blocked read/write/close or failed deletion
keeps its original thread, file descriptors and capacity debt until actual
settlement. Passing the deadline permanently fails that timing verdict even if
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
inconsistent or substituted ownership evidence
leaves debt and blocks reuse. An uncertain temporary state or moved payload needs
scoped reconciliation; no path-only deletion or blanket cleanup is attempted.

After actual release, keep the bounded receipt and ownership record as evidence.
The slot remains single-use; the implementation does not automatically retire
records or start another intake. Any later retirement or new allocation requires
the applicable scoped authorization and storage accounting.

## What the receipt establishes

Receipts are bounded and contain no source/destination paths or raw notices. They
bind the file byte count/hash, canonical source, metadata/credit snapshot, notice
count/hash, original acquisition/expiry, owner, retained work and actual release
or cleanup debt. Keep the original bounded manifest for notice review. The durable
state is an ownership ledger; the final process receipt additionally accounts for
descriptor closure and the worker's actual exit. A `pending_valid` observation
is time-specific and expires at the original deadline.

The copy is **pending review**. Complete-file and embedded-notice inspection,
recording correspondence, conflicts in credit evidence and an exact trusted
admission review remain separate. No availability, access-ready or complete-credit
claim is promoted. The reported **194 seconds** exceed the earlier **150-second /
28,800,000-byte PCM** decoder case. Intake neither truncates the recording nor
expands that case, and permits no native decoder execution.

The deterministic tests use small synthetic files, explicit read/deletion barriers
and an interrupted synthetic child process. They exercise partial copy/write,
mutation/substitution, repeated cancellation, mid-copy and pending expiry,
wall-clock rollback, failed close/deletion, failed state sync, restart binding and
original preservation. They establish local ownership behavior only, not music
quality, recording validity, Linux containment or release readiness.
