# Product requirements

This specification defines product behavior and acceptance criteria. Source permission, prompt selection and [Jamendo discovery with checked streaming](decisions/0013-jamendo-discovery-streaming.md) are accepted designs. Production now wires bounded discovery, pooled ranking, checked streaming and prepared local FMA through the station. Jamendo remains default disabled. Source compatibility and current-image release qualification remain incomplete; no listening evaluation is claimed.

## Product and engineering intent

SobaFM is a self-hosted Discord music bot with one station per server. A manager places it in a voice channel. Listeners ask for existing music with `/play`, or generated music with `/freestyle`. The bot provides good music, responsive controls, reliable continuous playback and honest now-playing information.

Use clear names, cohesive modules, useful types and explicit ownership. Comments explain decisions and constraints. Remove obsolete code and avoid speculative abstraction, duplicated state and unnecessary indirection.

Handle cancellation, provider failure and shutdown predictably. Choose the simplest design that satisfies the specified behavior and measurable operational requirements.

Implement the specified behaviors without importing unrelated platform code or unverified release evidence.

## Scope

- Linux/Docker is the only deployment target. Windows may host an editor; there is no Windows runtime, native package, compatibility layer, CI target or installation guide.
- `/play` prioritizes complete recordings with more strongly supported prompt matches, pooling admitted candidates from enabled sources before choosing. Jamendo has the explicit provisional first-play exception below. Length is a secondary preference. Listening comparisons are optional deferred evidence, not an implementation prerequisite.
- Use a short outcome-based roadmap with explicit acceptance evidence.
- Scheduled agent automation requires explicit maintainer approval.

Linux amd64 is the first release target. Native arm64 is on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60), [ADR 0004](decisions/0004-amd64-whole-service-adoption.md)) and is not a gate for Outcomes 4 or 5. Build and qualify each supported target natively; emulated execution does not establish native performance. Docker is the supported operator path. A Linux source workflow supports contributors and debugging without creating a second product configuration.

## Music selection and catalog preparation

Provide a curated, richly described selection from the enabled sources. The maintainer confirmed direct permission for Jamendo, ccMixter and Free Music Archive with attribution, no commercialization or monetization, and donations permitted. [ccMixter is not an enabled audio source](#first-source-evaluation). Recording identity, complete credits, service access and permitted storage still require evidence. No entire catalog is assumed to provide superior quality.

Keep FMA's admitted, operator-hosted library prepared ahead of playback under [ADR 0007](decisions/0007-fma-dataset-batch-catalog.md). For Jamendo, [ADR 0013](decisions/0013-jamendo-discovery-streaming.md) accepts validated musical intent, bounded search across the permitted source catalog and ranking alongside admitted untried FMA recordings. Refills reuse intent. Singles and album tracks are eligible; do not restrict discovery to featured tracks. Retain provenance, credits and useful descriptors, with source-specific admission and retrieval. Expensive analysis belongs in ingestion, not between a slash command and first audio. Keep this a small application capability, not a general data platform.

### First source evaluation

The [initial selection decision](decisions/0002-prompt-catalog-selection.md), as narrowly superseded for Jamendo by [ADR 0013](decisions/0013-jamendo-discovery-streaming.md), follows the maintainer's direction to progress without listening tests. Compare admitted candidates in one pool; never stop after a match from the first source. Report missing, partial, unavailable, disabled and empty partitions explicitly. A complete prepared snapshot is a bounded subset. Source-wide Jamendo search describes its reach, not exhaustive global ranking; an empty bounded window does not establish catalog exhaustion. FMA may still supply a match when Jamendo is unavailable.

Use deterministic regressions to establish validation and failure behavior; [matching quality](decisions/0006-gemini-play-matching.md) needs opt-in evaluation against the real provider. The [earlier comparison protocol](catalog-evaluation.md) remains available for optional future human enjoyment, fit and fidelity evidence. It is deferred, supplies no invented ratings, and is not a gate for this slice. Audio acquisition, source integration and deployment retain their separate authorization and qualification requirements.

| Candidate | Intended contribution | Retrieval and qualification boundary |
| --- | --- | --- |
| Jamendo | Source-wide discovery, including singles and album tracks, with bounded pooled ranking and no featured-only restriction | The accepted path uses checked transient streaming under ADR 0013. Evaluate `mp32` VBR for fidelity and reliability; do not assume the default `mp31` at 96 kbps is appropriate. One metadata request succeeded; no audio request occurred. [API documentation](https://developer.jamendo.com/v3.0/tracks) |
| ccMixter | **Not enabled; see below.** A complementary curated pool with editorial picks, vocals and instrumentals | Its public query API explicitly supports third-party applications, but its media route refuses non-browser clients. [Application/API statement](https://ccmixter.org/about) |
| Free Music Archive | Hand-picked albums, artists and genres in an operator-hosted library | FMA's API is closed; hotlinking is disallowed. Apps must host permitted copies themselves. Forwarding user searches to scrape FMA needs explicit approval. Use controlled ingestion, not live scraping from `/play`. [App-developer policy](https://freemusicarchive.org/app-developers). The current catalog is prepared from the [FMA research dataset](https://github.com/mdeff/fma), under the limits in [decision 0007](decisions/0007-fma-dataset-batch-catalog.md). |
| Incompetech, Musopen and directly opted-in artists/labels | Specialist instrumental/classical coverage or deliberately chosen music | These candidates require source-specific qualification. Verify recording-specific rights, source access and operational fit before adoption. Composition age alone does not establish recording rights. |

**ccMixter is not an enabled audio source.** Its media route returns HTTP 403 to non-browser clients, while a logged-out browser gets the same file ([diagnosis](https://github.com/slackysoba/sobafm/issues/111#issuecomment-6654375637)). The exact server rule isn't established. There is no scripted route without disguising the client or asking the owners for an exception, and automating a browser for a batch would be the same workaround. So on 2026-11-05 the maintainer [decided to skip it](https://github.com/slackysoba/sobafm/issues/111#issuecomment-6654419977), including a smaller hand-downloaded set. This is an access decision, not a change to the confirmed permission. The catalog schema and importer still accept ccMixter metadata, and a snapshot without it reports that partition as missing.

Do not assume Pixabay's standalone-use restrictions or video-focused NCS/other creator policies cover music-only Discord playback. Resolve any conflicting Scott Buckley usage terms before adoption. These sources remain excluded from the initial candidate set unless the concrete use is clarified.

### Catalog model and ranking

Keep a stable source identity, recording identity, title/creators, provenance, license/notices, permitted retrieval/storage policy, availability, duration/format and curation state. Keep authored descriptors and their confidence/provenance distinguishable from source tags. Deduplicate imported recordings where identifiable.

[Gemini ranks admitted recordings](decisions/0006-gemini-play-matching.md) a program hasn't played or tried against the request, [eight at a time](decisions/0011-program-refill.md), using title, creators, duration, descriptors and retained source details such as tags, tempo and album. Require complete recordings subject only to the Jamendo provisional rule below; reject identified previews, excerpts, snippets, stems, loops and sample packs rather than padding them. Duration alone never establishes completeness. Explicit duration, exact identity and exclusion wording informs the ranking and isn't a hard filter.

Preserve source-bound immutable credits and provenance. Provider/model metadata cannot approve itself. FMA keeps separate trusted reviews binding credit completeness and access readiness to the exact metadata digest; its source details remain matching hints outside that digest. Jamendo instead requires the operator-enabled local policy and both digest bindings below. Metadata fit is not an enjoyment or fidelity score. Recent-history diversity and exploration belong to a later continuous-station slice. A skip is weak evidence of dislike, and favorite/dislike commands remain a later enhancement.

Screen corrupt or incomplete files, unintended long silence and severe clipping separately from musical taste. Intentional lo-fi texture is not automatically a defect. Add automated instrument/vocal/tempo/energy descriptions or text/audio embeddings only if a bounded comparison demonstrates better matching that justifies dependency, compute and maintenance costs. Do not require CLAP, a vector database, GPU service or an LLM analysis call per track by default.

A musical request always gets the nearest admitted recording, and the reply says how close it is: the named recording, a close match or the nearest available. Where the catalog lacks a requested artist or title, play the nearest fit and say so; never present a substitute as the requested recording or promise mainstream catalog coverage. Catalog lookup must not inherit generated-music artist-imitation restrictions accidentally.

### Jamendo checked first-play admission

Apply [ADR 0013's full `jamendo-stream-1` rule](decisions/0013-jamendo-discovery-streaming.md#jamendo-only-admission-jamendo-stream-1), disabled unless the operator enables it. This Jamendo-only exception accepts an ordinary identified listing and normal stream with known duration and no contrary evidence as a provisional presumption, not proof of completeness or undisclosed rights. The maintainer accepted that the source may be wrong or omit a credit and that some audio may be heard before a later failure is detected. FMA's exact admitted list and manual reviews remain unchanged.

Require matching stable identity, nonempty title and creator, a validated source backlink, positive finite duration, a recognized Creative Commons license with exact version/jurisdiction and the ordinary streaming route. Reject known incomplete or non-recording forms, missing/invalid required identity, rights or credits, unreadable required notices and unresolved composition/sample-rights conflicts. Preserve all supplied creators, Jamendo provider credit, backlink, license and required notices without truncation. Missing optional album data for a single is allowed; no arbitrary 1–15-minute eligibility filter is adopted.

Bind each local decision to its recording key, canonical metadata digest, policy-evidence digest covering every admission input (including relevant source details and notices), policy version, reason and refresh time. Search-time decisions allow ranking only. Immediately before every audio open, refresh by ID and re-evaluate, even on a cache hit. Changed metadata/evidence/policy invalidates the old decision; a changed valid recording gets a new immutable decision and credits. Material identity/content changes discard the stale ranked choice. Failed refresh, removal or known rights conflict permits no audio.

Inspect bounded leading embedded tags before readiness and later notices as they arrive. Additional required notices or conflicts invalidate approval and stop the attempt rather than rewriting heard credits; unknown or oversized unreconcilable notices fail closed. Bind prepared audio and rendered credits to the refreshed decision. Only its actual source switch changes `/now`. This records application of the accepted policy, not independent source truth or legal assurance.

### Source-specific audio storage

Use transient streaming buffers for Jamendo, with only operationally necessary caching under its API terms; do not build an offline Jamendo library. Keep each track's license and provider obligations separate. [Jamendo API terms](https://devportal.jamendo.com/api_terms_of_use)

For sources permitting copies, support either bounded temporary acquisition followed by deletion or an operator-managed curated library, chosen according to the source and deployment. FMA-style imports require hosted copies; repeated download/play/delete is not a workaround for its no-hotlinking policy. Deleting a file never expands permission to use it.

Retain prepared FMA metadata. Jamendo starts with program-scoped candidate windows and only necessary decisions/credit provenance in a documented bounded journal, with no raw-response archive or mandatory new metadata database. Persist audio only where the chosen source and actual recording permit it, with a configured storage limit, validation, eviction/removal policy and clearly documented volume. Recheck availability or rights changes according to source obligations. Do not store durable Jamendo stream URLs or permanent signed/credential-bearing URLs. No listener-facing download, offline player or replay-cache product is included.

Operator-side audio persistence is limited to qualified imported sources. Record the enabled-source and storage decision before implementation. Missing required rights evidence or conflicting rights make a recording ineligible; the Jamendo rule's provisional reliance on published evidence does not certify undisclosed rights.

## Commands and access

Commands work in servers, not direct messages.

| Command | Required behavior |
| --- | --- |
| `/play request:<text>` | Accept up to 200 characters. Start or replace an indefinite existing-music program matching the request. A new program prepares while viable current audio continues. |
| `/freestyle request:<text>` | Accept up to 200 characters. Start or replace a generated program. The server's duration applies from acceptance, including startup. |
| `/skip` | Catalog mode only. Prepare the next eligible track under the current plan, without another model interpretation or a member queue. Preserve current audio if preparation fails and it remains playable. |
| `/stop` | End the program. Generated audio fades out; catalog performance ceases. Remain connected and idle. |
| `/now` | Private current-program information, including starting versus actually heard state. Show complete catalog credits or generated time remaining as applicable. |
| `/join` | Join or move to the manager's voice channel. |
| `/leave` | Stop and disconnect. |
| `/settings [duration] [volume] [cooldown]` | Show persisted server settings or change supplied values. |

`/join`, `/leave` and `/settings` require Manage Server by default. `/play`, `/freestyle`, `/skip` and `/stop` require the caller to be in the bot's voice channel; a manager may stop from elsewhere. Use Discord command permissions for administrative customization, not a second role system.

When the bot has not joined voice, playback commands explain that a manager must use `/join`. `/now` imposes no extra manager or voice-membership requirement. Verify access as an ordinary listener.

If catalog configuration is absent, omit `/play` and `/skip` from registration and remove stale registrations during synchronization. Generated mode remains available when its required configuration exists. Log a safe explanation.

Acknowledge or defer interactions within three seconds. Responses describe accepted actions accurately and never claim music is audible merely because a provider or producer started.

## Station behavior

Each server has one connected player and at most one desired program. There is no member queue or playlist. Separate desired intent, prepared audio and the source actually heard; do not duplicate these facts across loosely synchronized controllers.

A failed or cancelled replacement preserves current playable audio where possible. Rapid requests settle deterministically. Stale callbacks cannot replace the newer request's state, release its cooldown or announce its credits.

Generated programs last the configured duration, restarting that duration on a new accepted generated request. Catalog programs continue until stopped, replaced, voice is lost, listener grace expires, or selection reaches its finite recovery, history, cycle or quota bound. Exhausting a fixed prepared catalog may end a program; exhausting one Jamendo search window is not source-catalog exhaustion. Preserve [ADR 0011's refill behavior](decisions/0011-program-refill.md) with [ADR 0013's discovery bounds](decisions/0013-jamendo-discovery-streaming.md#search-ranking-and-bounded-continuation).

End the program 60 seconds after the last listener leaves. A listener is neither a bot nor deafened. Cancel the grace period if a listener returns. Stay connected and silent after the program ends.

Persist the selected voice channel. On restart, rejoin if it exists and the bot can connect and speak; otherwise forget it. Do not resume the previous program. Adopt an administrative move. On disconnection or channel deletion, end the program and forget the channel.

Generated music is instrumental, optionally with wordless vocals. Catalog music may contain lyrics. Generated-to-generated transitions use fades/crossfades. Pending a source-specific revision, catalog boundaries use successive playback with no automated fade, overlap, looping, time/pitch changes or expressive editing. Normal non-clipping volume remains available. Explicit skip, replacement and stop may cease a catalog performance.

Bound catalog selection attempts, retries, prefetched work and no-repeat history. Keep at most the audible track and one prepared next track. Exhaustion produces a useful explanation and bounded recovery, not a hot search loop or automatic history clearing to repeat the same tracks.

Each Jamendo selection attempt uses at most two sequential pages of 100 and a 1 MiB limit per metadata response, advancing a bounded cursor across refills and deduplicating stable IDs. Keep the 256-record/source envelope and 512,000-character matching ceiling; reject/degrade with explicit coverage rather than truncate credits. Keep eight choices per list and three refill attempts with the existing backoff; search counts inside each attempt, with no multiplied retry loops. End before evicting from the 768-key played/tried history, at 768 selection cycles, or after three no-progress attempts, with an honest reason. Count calls and bytes, honor actual account/provider restrictions and operator-configured limits. No 2 GB/day or 30,000-call/month allowance is accepted.

## Interpretation and selection

Use Gemini to turn a request into a validated mode-specific plan. For Jamendo-enabled catalog mode, a new request uses one musical-intent interpretation, bounded source search and one ranking call over admitted Jamendo and FMA candidates. Refills reuse intent and search/rank when the last recording on the list starts, within the existing three-attempt/backoff rule. Keep one selection pipeline per station and the shared four-model-call semaphore, with existing per-call bounds. No agent conversation or arbitrary tool use is included. Validate output before it changes state or causes provider activity. The implemented local-FMA path still makes one ranking call initially and one per refill attempt.

Generated plans include a short title, one to four weighted styles and supported optional tempo, key, density, brightness, drum and wordless-vocal controls. Use relative context only within the same mode; switching modes starts a fresh interpretation and explains that behavior where needed.

Requests unrelated to music, prompt-injection attempts and disallowed requests leave current music unchanged. Do not hand model output arbitrary tool access.

On generated interpretation failure, use the original musical request as a simple generation direction and disclose the fallback. Catalog interpretation failure preserves the current program and explains the problem; do not send unvalidated raw text into a provider-specific search path. When a later call for the same program fails, the current recording finishes and, after bounded retries, the program ends with an explanation.

Generated requests for an artist's style should be translated into descriptive musical characteristics. Existing-recording search follows the curated catalog policy above; do not prohibit legitimate artist/title lookup by reusing generative-music restrictions.

Only send the request, necessary current musical context or validated intent and, for catalog ranking, descriptive metadata about admitted recordings to the model provider. Jamendo receives validated musical query fields, without Discord identity; disclose this additional musical search and the repeated source/model calls during refill. Do not send audio, Discord identities, credit snapshots or credentials in model prompts. Keep `store=false`; it does not remove Google's unpaid data use or human review. Existing provider, privacy, audience and no-spend choices remain in [provider access](provider-access.md).

Pin a supported current model/API after checking official documentation and performing the applicable opt-in evaluation. Treat dated model names, timeouts and provider observations as assumptions requiring relevant validation. Avoid changing models without rechecking behavior.

## Persisted settings

| Setting | Default | Allowed range and scope |
| --- | --- | --- |
| Generated duration | 60 minutes | 5–240 minutes; generated mode only |
| Volume | 50% | 1–100%, linear gain; shared across modes |
| Change cooldown | 30 seconds | 0–600 seconds; server-wide across play, freestyle and skip |

Check cooldown before interpretation or selection. A failed or no-op request may release only its own cooldown.

Use SQLite for the small persisted state: server settings and remembered voice channel. Use standard-library support unless a concrete need justifies more. Keep database work off the audio callback and avoid event-loop blocking. Do not introduce an ORM, remote database or migration framework without a demonstrated need.

## Playback implementation constraints

Prefer established libraries and Linux facilities. Choose dependencies for maintenance, licensing, security and operational fit. Prefer a maintained decoder distribution and supported SDK over custom builds or forks unless a documented requirement justifies them.

Decode incrementally away from the event loop and the voice callback. The callback consumes prepared PCM without blocking on network, process I/O, database operations or cleanup. Centralize the PCM format: stereo signed 16-bit at 48 kHz, with 20 ms frames.

Readiness means sufficiently buffered, admitted playable audio. The initial readiness target is six seconds of pre-roll; a shorter recording requires verified completion before admission. Document any source-specific change rather than weakening readiness implicitly.

Track transfer completion, decoder completion, valid PCM and audible drain separately. Clean transport completion and successful decoder termination do not alone prove all expected audio was received. Conversely, inaccurate metadata must not become an unquestioned duration oracle. Establish a finite completion policy from the chosen source's guarantees and test truncated, corrupt, oversized, stalled and cancelled streams.

Jamendo's provisional first play preserves six-second readiness and completed verification for shorter recordings. Its transport slice must fix finite encoded/PCM/time/notice bounds and conservative duration tolerance before becoming Ready, covering HTTP framing, MP3 validity, decoded-duration consistency and bounded silence/clipping checks. A later integrity, severe-defect or rights/notice failure stops emission and discards that stream's buffered audio, retains failure and cleanup ownership, marks the attempt failed/tried and uses bounded continuation. No automatic restart, padding, looping or claim of full playback is allowed. Quarantine semantic/rights conflicts until corrected evidence or operator review; transient failures are retryable only through later bounded attempts without clearing program history. Preserve other playable audio when a replacement fails before switching and preserve local-file size/hash verification.

Use hard finite bounds for input, output, queues, metadata, operations, concurrency and retries. Reserve payload capacity before allocation or transfer. Child processes must not outlive their accountable owners unnoticed.

Cancellation never implies a resource was freed. Join retained operations before replacing them. Retain one committed terminal response and actual transfer ownership. Avoid dispatcher deadlocks and grant/cancellation races. Use the engineering notes' failure schedules as regression cases without prescribing an unnecessary IPC protocol.

### Approved memory definition

**Accepted three-station numeric contract:** [ADR 0012](decisions/0012-three-station-capacity.md) supersedes the earlier below-100,000,000 controlled-byte and at-most-34,560,000 PCM limits under the linked maintainer acceptance. The selected route is one bot identity with at most three simultaneous guild stations. Its accepted contract is 268,435,456 whole-service group + 13,824,000 PCM banks + 12,582,912 copies = **294,842,368 controlled bytes**, charged in full to every playing server, never divided by three. Conditional aggregate PCM is **at most 101,552,640 bytes**, with at most six PCM-bearing native spaces under the unchanged per-decoder bounds. Each station exclusively reserves two producer/bank/copy slots and its voice-copy credit before interpretation. A fourth station is refused before provider work. Pending requests, prepared replacements, fades, retained native/session owners, bank views and voice debt remain charged until actual release. No cross-guild borrowing is allowed. These are accepted capacity guarantees, not measured fit or release evidence; the old guarantees remain historical only. Actual-image three-station qualification is pending.

This is a controlled-capacity guarantee, not a total RSS or total physical-audio-memory ceiling. Parent allocator/object overhead, native voice encoding/encryption/packet backing, kernel pipe/socket storage, the narrow decoder kernel PTY backing defined in [ADR 0003](decisions/0003-decoder-pty-exclusion.md), and uncharged shared/mapped backing are explicitly excluded; total physical audio memory may exceed the controlled-capacity total. The PTY exclusion does not cover userspace audio, diagnostic, application or IPC payload. Repeated sessions may leave an unquantified physical/kernel backlog; no three-second kernel-release bound or eventual reclamation result is established. Keep exclusions visible in the operator and developer documentation.

Derive a feasible configuration and qualify it in the intended container before making it an application-wide dependency. No baseline subtraction or unallocated “residual allowance” may disguise capacity. If the guarantee prevents a useful deployment, raise one concrete decision with a measured alternative before proceeding.

Containment must take effect before untrusted input reaches a worker or decoder. Resource containment is not a claim of complete security isolation. If cgroups are selected, demonstrate actual delegation, namespace reachability and migration permission in non-root, default-seccomp Docker. Do not silently require privileged containers, a Docker socket, host-wide writable cgroup mounts or host security changes.

Under the [accepted amd64 adoption](decisions/0004-amd64-whole-service-adoption.md), which follows the [approved #7 experiment](decisions/0001-linux-resource-proposal.md), the Docker-owned whole-service group is the shared whole-worker accounting unit, charged in full to each playing server alongside parent PCM/copy reservations. Demonstrate runtime authority and placement before input, effective namespace reachability and limits, and inherited containment of descendants; delegation or migration by the non-root application is not required for this interpretation. Adoption permits building the first eligible-recording playback slice on amd64; it is not a qualified application architecture. The accepted supersession in ADR 0012 changes only capacity numbers and station admission. Cleanup, security, source-access requirements and native targets remain unchanged.

### Cleanup

Use one cleanup owner and one original monotonic three-second total deadline for feed cancellation, closure, joins, bounded drains, process-tree termination and reaping. Repeated cancellation does not reset the deadline or abandon ownership.

Deadline expiry is a failed cleanup verdict. Retain leases, debt and process slots until resources actually release. Do not free capacity merely to allow the next request. A timeout must leave the station's health and admission behavior understandable.

Under the narrow decoder-PTY decision, a subsequent pair may be admitted only after actual userspace, process and file-descriptor settlement, subject to all other admission and qualification gates. This does not claim that prior excluded kernel objects were reclaimed. The original cancellation deadline, failed verdicts and retained userspace ownership/debt remain; close, EOF or process exit alone cannot certify all required settlement. The historical experiment remains bound to 90,177,536 group + 4,608,000 PCM + 4,194,304 copies = 98,979,840 bytes. ADR 0012 separately states the accepted three-station contract; neither design introduces a new PTY pool.

## Feedback, metadata and credits

Show the connected player's heard source. A pending replacement must not change heard-source title, credit history or `/now` output prematurely.

Catalog admission requires a source-bound immutable credit snapshot containing the recording title, supplied creators, provider/backlink and all notices and license information required by the actual source. Validate provenance and resolve conflicting or missing required information before playback. Jamendo's sole exception provisionally relies on the checked listing and available notices under `jamendo-stream-1`, with later notice failures stopping playback as specified above. Do not equate an API flag or local policy decision with complete permission.

Private `/now` makes full credits available to ordinary listeners. Keep credit rendering, pagination and retained views bounded, caller-bound and expiring. Never silently truncate legally required notices. If complete safe credits exceed supported bounds, reject the candidate. Do not add a public-message fallback or new Send Messages permission just to display credits.

Voice-channel status is best effort. A missing status permission must not break playback. Avoid per-track public chat spam.

Escape provider/model text, strip bidirectional controls, disable mentions and previews, and render only validated credential-free links. Use concise actionable error categories for configuration, permission, quota, outage, no fit and exhausted recovery. Do not expose raw exception bodies or provider URLs containing secrets.

## Deployment, privacy and supply chain

Provide one documented Docker/Compose deployment, a persistent `/data` volume, graceful signal handling, an init process and a sensible restart policy. Run non-root with default Docker security settings. The operator should not need a compiler.

Use environment variables and placeholder-only examples. Discord, catalog-matching and generated-mode credentials are required for their respective capabilities; source-specific catalog configuration follows the final provider decision. Never commit or read real `.env` files while developing. Do not put credentials in child arguments, logs, prompts, published diagnostics or documentation.

Request only the necessary Discord Guilds and Voice States intents. Explain installation permissions and ordinary-listener command access.

Send structured, safe logs to stdout. Log enough to distinguish provider, decode, voice and cleanup failure without raw stream data, secret-bearing URLs or interaction tokens. Request text is debug-only and opt-in. Avoid building a separate telemetry service.

Document current provider access, privacy, cost and deployment eligibility from authoritative sources. Verify current terms at the time of the build. Account access and playback permission are separate. Obtain any required actual maintainer decision for a new service, spend or legal/security exception; do not infer it from tool availability.

Use bounded transient playback storage and source-specific acquisition as described above. Qualified operator-side catalog imports may persist authorized audio. Listener-facing downloads, offline playback and a general replay-cache feature remain out of scope.

Use a maintained, license-compatible decoder distribution and document redistribution/source obligations. Record exact dependency versions and relevant native components. Python dependency scanning alone does not cover native libraries. An SBOM and update process should reflect the actual release, without creating bespoke package infrastructure unnecessarily.

Source-access constraints remain in force unless directly changed by the maintainer. Do not read, acquire, hash, reconstruct or build `fftools/ffmpeg_filter.c` or `18.raw` through local tools, archives or CI. Resolve any selected build path that necessarily touches restricted material before executing it.

## Acceptance and evidence

Prove behavior on the deployment target, not only through fakes.

| Area | Release evidence |
| --- | --- |
| Catalog selection | Deterministic evidence establishes output validation, source-specific admission (including the explicit Jamendo provisional exception), disclosed fit and honest coverage. Opt-in provider evaluation establishes that real requests rank sensibly. Actual availability, stream semantics and integrity require source integration evidence. Subjective enjoyment and fidelity remain unmeasured until an optional authorized listening evaluation; preserve poor matches and unavailable cases. |
| Command/access behavior | Both modes, all controls, cooldown ownership, manager overrides and regular-listener now-playing access work as specified. |
| Continuity | A 60-minute program has at most two seconds of total underrun under normal service conditions. Measure both applicable modes. |
| Latency | Median request-to-audible music is at most 15 seconds for generated playback and admitted catalog play requests; measure catalog skip separately against the same target. Report admitted failures/timeouts separately. |
| Transitions | Replacements, skips, session renewal, listener grace, admin moves and voice loss settle correct state and heard credits. |
| Capacity and cleanup | Actual Linux containment, useful workloads and release behavior meet the approved metric, including failure/cancellation debt. |
| Deployment | Native amd64 runs the intended image non-root; persistent settings, restart and clean shutdown work. Native arm64 is on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60)). |
| Guide | A clean Linux/Docker walkthrough reaches both existing and generated music, controls, credits and restart using the documented setup. |

Unit and component tests should target invariants and important schedules. Regression tests reproduce failures without sleeping until a race happens. Test at real boundaries where a fake-only test would miss integration behavior.

Provider evaluations and real Discord tests are opt-in and excluded from CI. Use bounded, explicitly authorized live tests; never start listening sessions, paid calls or source acquisition merely because tests exist. Small owned offline fixtures and fixed loopback fakes are appropriate for ordinary CI.

Bind checks, review and release evidence to the actual commit. Do not substitute another platform, source revision or image's results for the required qualification. Reuse valid unchanged checks.

## Repository organization and working standards

Start with a navigable structure:

- `src/sobafm/`: application modules organized around real responsibilities.
- `tests/`: meaningful unit, component and integration tests, with opt-in live tests clearly separated.
- `docs/requirements.md`: the authoritative product contract.
- `docs/architecture.md`: a short explanation of actual responsibilities, data flow, ownership and important bounds.
- `docs/decisions/`: short decision records for consequential product/architecture/security/service choices.
- `docs/roadmap.md`: outcome-based milestones and their GitHub issue links.
- `docs/operations.md`: installation, configuration, recovery, updates and troubleshooting.
- `.github/`: CI and concise contribution templates.
- `AGENTS.md`, `CONTRIBUTING.md`, `README.md`, `LICENSE`: discoverable standards, setup, product introduction and MIT license.

Do not create empty placeholder layers to make this structure look complete. README should show what the bot does, how to run it and where to find the design.

Use Python 3.14, complete typing and strict Pyright. Keep dependencies locked with uv. Use Ruff, pre-commit, Markdown checks, secret scanning and pytest. Before a push run locked environment synchronization, all pre-commit checks and the applicable full offline suite.

Requirements and accepted decisions outrank code. An issue and its parent give scoped intent. Chat history, private receipts and agent memory are not the project's durable specification.

Use one issue, one branch and one pull request. Branches follow `<type>/<issue>-<slug>`. Make commits and PR descriptions concise and technically precise, describing behavior, scope, rationale and verification. Agent commits carry a model-identifying Co-Authored-By trailer.

Work autonomously within a Ready issue and its approved design. Ask the maintainer only for a material product/architecture decision, consequential irreversible action, legal/security exception, cost/vendor commitment or missing credential. Put the concrete question and options on the issue, label it maintainer-approval and mark it Blocked. Do not ask again for an authorization already recorded.

Require independent review by a separate agent for agent changes before merge, with blocking findings resolved publicly. All required checks must pass. Maintainer-approval changes require actual maintainer acceptance. Squash merge; never push directly to main after bootstrap or rewrite shared history. Preserve branches used as bases until dependents are rebased. Delete superseded implementation with the replacement and update affected docs in the same PR. Accepted ADRs are superseded by new records.

## GitHub roadmap

Organize the Project by useful outcomes, with Backlog, Ready, In progress, In review, Blocked and Done statuses. Track priority, milestone and dependencies. Keep the current slice concrete and the distant roadmap concise.

1. **Source and feasibility established.** Integrate the music-source decision, assess real selection and rights, and validate the chosen Linux resource/deployment route. Exit with one source strategy, one feasible deployment configuration and bounded success criteria.
2. **Existing music works end to end.** A narrow Linux container slice joins voice, plays an eligible recording, shows correct credits, stops and shuts down. Then add request matching, continuous selection, skip and replacement behavior.
3. **Generated music and one station.** Add validated interpretation, Lyria continuity, generated duration, fades and safe mode changes while retaining a single clear station model.
4. **Operator-ready bot.** Persist settings/channel, finish permissions and feedback, handle recovery, package Linux amd64 (native arm64 is on hold: [#60](https://github.com/slackysoba/sobafm/issues/60)) and complete security/dependency checks.
5. **Qualified release.** Run bounded real-source/Discord acceptance, endurance and latency measurements, memory/release qualification and a clean-machine BOTH-mode guide walkthrough. Publish only verified claims.

Use a parent milestone issue for each outcome and a small number of implementation issues with acceptance criteria and dependencies. Detail the current and next slice; do not pre-create a large chain for hypothetical internals. Keep decision blockers separate from active implementation. Close issues only when their acceptance evidence exists.

## Explicit non-goals

No Windows support; member queues or playlists; voice input; generated lyrics; Stage channels; more than one voice channel per server; hosted public service; web dashboard; download/offline catalog feature; speculative plugin framework; custom permission system; or automatic paid/provider activity.

Catalog selection quality and variety are explicitly in scope.

## Evidence status

This specification is a product contract, not release evidence. The metadata-selection strategy is accepted, with ccMixter not enabled as an audio source; recording admission, source integrations and Linux deployment feasibility still need their applicable evidence. Verify provider terms and material API behavior against current authoritative sources when implementing each integration.
