# Fixed ccMixter source-acquisition proof

Issue [#19](https://github.com/slackysoba/sobafm/issues/19) implements the
[candidate's finite source contract](ccmixter-playback-candidate.md#proposed-finite-source-proof).
This is an opt-in Linux evaluation, with no production source integration,
decoder, new dependency, catalog admission or playback. No live result is
recorded here. Human listening remains deferred.

## Implemented boundary

`sobafm.ccmixter_proof` checks the six fixed metadata responses and can make
one GET for upload **70993**, file **129564**, expecting **4,693,829 bytes**.
There are **zero retries**, including after a transport or 5xx failure. This
is stricter than the contract's allowance of at most one transient retry.

Each response has one owned Python child. The supervisor reserves its sole
request/file slot before starting, waits on actual process exit, joins it,
closes handles and verifies file removal before releasing that slot. Delivered
stdout or HTTP EOF does not release ownership. Linux pidfds and a 17-byte
shared progress/count/deadline record keep supervision independent of blocked DNS,
socket I/O and result delivery. The child publishes the original cleanup
deadline before HTTP close and wakes the supervisor through its bounded result
channel; close cannot silently inherit a longer request deadline. Children
create no descendants.

Connect, inactivity and absolute request limits remain **10, 20 and 120
seconds**. The supervisor separately enforces the connect and absolute limits;
the child uses socket inactivity timeouts and an absolute alarm. The run is
limited to **600 seconds**, shortened by the review window. Signal cancellation
wakes the supervisor. One original **three-second cleanup deadline** covers
kill, join, close and removal. Missing that deadline permanently fails the
cleanup verdict, even if resources are later released.

Metadata has six requests, **500,000 bytes each**, **3,000,000 total**. Audio
has one attempt and **10,000,000 bytes** reserved for body transfer and storage;
the contract's **20,000,000-byte** total can never be exceeded by this single
attempt. Counters charge body bytes returned to the application, including
partial/failed responses. An in-flight read prepays its full remaining chunk
capacity before blocking; only its completion refunds unused bytes. Timeout or
death before the count commit conservatively retains that charge, so receipt
totals may exceed the separately reported bytes actually delivered. These are
not TLS, HTTP-header or wire-byte meters.
Reads never request bytes beyond the remaining cap. A response that reaches
the cap without confirmed framed completion is rejected without an overflow
probe. Headers can reject a size before any body read, but cannot establish
completion. Chunked and connection-close framing are bounded too.

Unexpected status, redirects, authentication, partial responses, duplicate or
ambiguous framing, content encoding, wrong content type, changed identity,
route, size or metadata stop the proof. The client does not use environment
proxies, follow redirects, authenticate, search, paginate or seek alternatives.
TLS uses Python's normal certificate/hostname verification. The fixed network
entry requires `--live-ccmixter`; importing the module does no work.

## Separate metadata execution review

The run requires an operator-controlled JSON manifest with this exact shape:

```json
{
  "schema": "sobafm-source-proof-review-1",
  "candidate_digest": "REPLACE_WITH_METADATA_DIGEST",
  "expires_at": "REPLACE_WITH_UTC_WINDOW_END",
  "review_reference": "https://github.com/slackysoba/sobafm/issues/19#issuecomment-REPLACE",
  "responses": {
    "mix_page": "REPLACE_WITH_SHA256",
    "parent_page": "REPLACE_WITH_SHA256",
    "root_page": "REPLACE_WITH_SHA256",
    "info": "REPLACE_WITH_SHA256",
    "terms": "REPLACE_WITH_SHA256",
    "about": "REPLACE_WITH_SHA256"
  }
}
```

This placeholder is intentionally invalid and is not an execution grant.
The coordinator's concrete execution review must bind the manifest, candidate
revision, execution commit, exact image/native target, responsible owner,
storage checkpoint and one window ending at most ten minutes after invocation.
Use `metadata_digest()` on the prepared recording from the existing catalog
parser. No admission review is produced or changed.

Derive response hashes from the exact saved, reviewed, unencoded UTF-8 bodies
for the six URLs in `METADATA`. Review the three supplied credit pages, their
complete chain and license/notice statements, the current terms/access pages,
and the exact info record against the candidate document. Check the single
file's `download_url` and numeric `file_rawsize` fields against the stated
route and size. The existing saved projection deliberately omitted those
fields; owned fixture tests do not establish the live API shape. If the saved
response does not supply those fields as required, resolve the discrepancy
before a live run. Do not invent a current fingerprint or silently substitute
new metadata. Do not use provider-generated approval fields. The preserved
3,624-byte info response from the candidate preparation confirms
`file_rawsize: 4693829`; `file_filesize` is only display text (`" (4.48MB)"`).

This exact-body comparison is a conservative safeguard for **one finite
proof**, not the intended long-term ingestion policy. It also detects changes
outside known fields without an HTML scraper that could silently discard a
new notice. Dynamic page content may cause a safe rejection. A mismatch means
**changed metadata requiring review**, not a finding that rights were revoked.
The receipt retains only request name, digest, count and verdict. It does not
export response bodies or fetch media after a mismatch. Acquiring/reviewing a
new baseline needs its own applicable bounded metadata scope; do not add
unmetered requests to this run to make the comparison pass.

The info response also passes the existing importer and an exact comparison
with the saved projection: title, uploader, reported format/duration, license,
file identity and direct parent must remain unchanged. All three page/policy
fingerprints must match before media access. The immutable prepared candidate
and trusted catalog-review boundary remain unchanged and unadmitted.

## Concrete command for execution review

**Do not execute this command merely because it is documented or merged.**
The initial issue authorizes code and owned offline/loopback tests only. The
coordinator releases a reviewed, finite live invocation separately. There is
no decode step; #7's binary, PCM and useful-fit gates remain independent.

This proposed native amd64 invocation uses the already-present Python image
from the reviewed #7 experiment, without pulling or building. Reconfirm its
exact Python version, image digest and native target during execution review.
Keep the current **90,177,536-byte** group cap, zero additional swap, non-root
user and default seccomp. This does not claim the source workload fits, or
qualify containment or PCM. Native arm64 needs its separately reviewed image
and actual target; do not relabel emulation as native evidence.

From a clean, verified checkout, with `REVIEW_FILE` pointing to the private
reviewed manifest, prepare the following command. All mounts are read-only
except the private audio tmpfs; no environment file or credential is used:

```sh
revision=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
docker create --name sobafm-source-proof-19 --pull=never --init --restart=no \
  --label org.sobafm.project=sobafm --label org.sobafm.issue=19 \
  --label "org.sobafm.source=$revision" \
  --user 1000:1000 --cap-drop=ALL --security-opt=no-new-privileges=true \
  --read-only --cgroupns=private --network=bridge \
  --memory=90177536 --memory-swap=90177536 --pids-limit=16 --cpus=2 \
  --log-driver=local --log-opt=max-size=100000 --log-opt=max-file=1 \
  --tmpfs /run/sobafm-source-proof:rw,noexec,nosuid,nodev,size=10000000,mode=0700,uid=1000,gid=1000 \
  --mount "type=bind,source=$PWD/src,target=/proofsrc,readonly" \
  --mount "type=bind,source=$PWD/evaluations/catalog,target=/catalog,readonly" \
  --mount "type=bind,source=$REVIEW_FILE,target=/review.json,readonly" \
  --env PYTHONPATH=/proofsrc --entrypoint=python \
  ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f \
  -B -m sobafm.ccmixter_proof --live-ccmixter \
  --review /review.json --catalog-directory /catalog
```

Before create, confirm the name is absent and Docker is reachable. Bound each
management call to eight seconds (plus at most two seconds kill grace), or the
shorter remaining window; never use a second create after an uncertain result.
Recover the actual container ID by inspection only when all expected ownership
labels, image and source revision agree. Never adopt an unrelated container.
Record the exact mounted source hashes and candidate/review digest privately.
Do not publish personal paths or session identifiers.

The assigned supervisor starts the verified ID with `docker start --attach`
within the approved ten-minute window. It caps new evidence at **100,000
bytes** and captures only bounded JSON/status/inspection output. The child's
ordinary deadline is shorter than this host window, reserving time for cleanup.
Set the manifest's expiry accordingly. The root filesystem is read-only,
bytecode writing is disabled and audio never enters a bind mount, Git, logs or
exports. Do not add `--rm`: a deadline is not evidence that ownership ended.

## Cleanup and recovery

On failure/cancellation the owner immediately stops the child, joins it and
unlinks the sole `body` file. No next request starts until actual release. A
failed cleanup retains the child/count mapping, handles, file reservation and
exclusive run lock. It emits one safe debt checkpoint, keeps trying release
without restarting the original deadline, and makes no further requests.
A pre-existing `body` file is unresolved debt and blocks a new run.

The host supervisor remains responsible if the process/container dies, loses
contact or is unresponsive. At the window end, send TERM to the verified
container and allow the original cleanup interval; inspect actual state.
Keep the ID, leases and debt until the container is confirmed stopped with
PID zero and the private tmpfs released. After preserving bounded evidence,
remove only that stopped, verified container and verify absence. Do not start
a replacement while that evidence is missing. The **one-hour absolute audio
retention backstop** requires another explicit cleanup/inspection attempt and
an actionable retained-debt report if release still cannot be verified; it
never authorizes pretending that a timed-out deletion succeeded. Keep any
retained location private. No broad cache pruning or unrelated cleanup.

Before execution and after cleanup, remeasure storage against the existing
**10 GB** development budget, **8 GB** heavy-work stop, **25 GB** minimum C:
free space and **500 MB** retained-artifact cap. These are distinct from the
runtime group/audio limits. This implementation installs no dependency and
creates no native build or audio fixture.

## Verdicts and offline verification

Receipts separate transport EOF, received/stored bytes, SHA-256, cleanup
release and cleanup-deadline compliance. A local hash identifies bytes; it is
not an authoritative content checksum. A successful transport does not prove
an MP3 is intact, has a complete ending, or is the expected recording.
`file_integrity`, `decode` and `embedded_notices` remain **pending**;
`audible_drain` remains **unobserved**, and admission is **not granted**.
No duration, exit status or near-duration result promotes those verdicts.

Ordinary `uv run pytest` includes only owned local HTTP bodies and deterministic
process/failure schedules. It tests caps, truncation, status/framing/content
conflicts, stalls, cancellation, delivered-but-live ownership, original cleanup
deadlines, late release, removal failure, metadata changes and unchanged catalog
rejection. These fixtures are deliberately not audio and do not establish a
live access pass, listening quality, decoder correctness or Linux containment.
Run locked sync, all-file pre-commit and the complete offline suite before
push; bind CI and independent review to the actual PR commit.
