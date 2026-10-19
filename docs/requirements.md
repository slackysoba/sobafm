# Requirements

- **Status:** Approved v1 baseline (#5), with M5 targets from accepted ADR-0006 and ADR-0007 (#119).
- **Related:** [Architecture](architecture.md) · [Roadmap](roadmap.md) · [Decisions](decisions/README.md)

Each requirement has a stable ID and a delivery milestone. M1–M4 describe the v1
baseline; requirements marked M5 describe the accepted target, not delivered
catalog behavior or passing qualification. Earlier milestone tags preserve the
origin of revised requirements. The accepted decisions authorize implementation;
the [qualification gates](architecture.md#qualification-and-evidence) still gate
catalog runtime readiness and shipping.

The #123 command migration delivers `/freestyle` for generated music while
temporarily retaining `/play` as its explicit generated alias. #128 replaces that
alias with the catalog command and implements CMD-9's registration rule. The M5
catalog requirements below remain targets during this intermediate step.

## Product summary

SobaFM is a self-hosted Discord bot with one station per server. A server manager
places it in a voice channel, where it waits for requests. In M5, `/play` asks for
matching openly licensed Jamendo tracks, and `/freestyle` keeps v1's generated
Lyria RealTime music. Gemini interprets requests for either mode. Catalog programs
play indefinitely; generated programs keep the server's play duration. Each
operator runs their own instance with their own Discord application, Gemini API
key and, for catalog mode, Jamendo application client id.

## Commands

- **CMD-1** `/play request:<text>` starts or replaces a catalog program from a request of up to 200 characters. V1's generated `/play` behavior moves to `/freestyle` (CMD-7). _(M2; M5)_
- **CMD-2** `/stop` ends the current program; generated audio fades out, and catalog audio ceases without a fade. SobaFM stays in the channel. _(M2; M5)_
- **CMD-3** `/now` privately shows the program's title, interpreted direction and requester, distinguishes starting intent from audio actually heard, and shows time left only for generated music. For catalog audio it provides the complete source-bound credits in FB-5/FB-6. _(M3; M5)_
- **CMD-4** `/join` connects SobaFM to the caller's voice channel, or moves it there; `/leave` ends any program and disconnects. _(M2)_
- **CMD-5** `/settings [duration] [volume] [cooldown]` shows the server's settings, or changes the given ones. _(M3)_
- **CMD-6** Commands are available in servers only, not in direct messages. _(M2)_
- **CMD-7** `/freestyle request:<text>` starts or replaces a generated program from a request of up to 200 characters, preserving v1's interpretation, duration, instrumental/wordless-vocal options and slow-start feedback. _(M5)_
- **CMD-8** `/skip` applies only to catalog programs and shares the change cooldown. Prepare a ready replacement while current audio continues, then switch under PLAY-9. It makes no new model call and creates no queue. A failed or no-op skip releases only its own cooldown. _(M5)_
- **CMD-9** Without runtime catalog configuration, do not register `/play` or `/skip`; synchronization removes stale registrations and startup logs a fixed explanation. `/freestyle` remains available with the required Discord/Gemini configuration. `/play` never silently selects generated music. _(M5)_

## Access

- **ACC-1** `/join`, `/leave`, and `/settings` require the Manage Server permission by default. _(M2)_
- **ACC-2** `/play`, `/freestyle`, `/skip` and `/stop` require the caller to be in SobaFM's voice channel; members with Manage Server may use `/stop` from anywhere. _(M2; M5)_
- **ACC-3** Server administrators adjust who may use each command through Discord's command permissions (Server Settings → Integrations); SobaFM has no separate role setting. _(M2)_
- **ACC-4** When SobaFM is not in a voice channel, playback commands tell the caller to ask a server manager to use `/join`. _(M2; M5)_
- **ACC-5** `/now` adds no manager or voice-membership check. Operators provide intended listeners an accessible channel where Discord permits the command, and verify ordinary-listener access before qualifying the deployment. Administrators retain Discord command permissions. _(M5)_

## Playback

- **PLAY-1** A server has one station/player and at most one desired program. A new request prepares a replacement while retaining the program still heard; transitions follow PLAY-9. There is no member queue or playlist. _(M2; M5)_
- **PLAY-2** A generated program plays for the server's play duration from station acceptance, including startup time, restarted by each new generated request, then fades out. A catalog program has no deadline: it continues until replaced, stopped, listener grace expires or voice is lost. _(M3; M5)_
- **PLAY-3** Playback is continuous: Lyria RealTime session limits and reconnections cause no audible gaps under normal service conditions. _(M2)_
- **PLAY-4** A program ends 60 seconds after the last listener leaves the channel. A listener is a member who is not a bot and not deafened. _(M2)_
- **PLAY-5** After a program ends, SobaFM stays in the channel, silent, until the next request. _(M2)_
- **PLAY-6** SobaFM remembers its voice channel. After a restart it rejoins that channel if the channel still exists and SobaFM can connect and speak there; otherwise it forgets the channel. Programs do not resume after a restart. _(M2)_
- **PLAY-7** If an administrator moves SobaFM, it adopts the new channel. If SobaFM is disconnected or its channel is deleted, the program ends and the channel is forgotten. _(M2)_
- **PLAY-8** Generated music is instrumental, with optional wordless vocals. Catalog tracks may have lyrics, as requested. _(M3; M5)_
- **PLAY-9** Every boundary involving catalog audio uses successive playback: outgoing audio ends before incoming audio starts, with no automated catalog fade or overlap and no intentional gap at a normal boundary. Catalog tracks normally play through verified completion and audible drain; explicit skip, replacement or stop ceases the performance. Generated-to-generated fades/crossfades remain unchanged. Ordinary non-clipping server volume is available; catalog remixing, expressive edits, time/pitch changes, loops and loudness processing are excluded. _(M5)_
- **PLAY-10** Catalog completion requires valid finite positive metadata duration of at most 900 seconds, clean HTTP EOF and agreement with any declared length, stdout/stderr EOF and drain, zero decoder exit and reaping, nonempty stereo-sample-aligned output, and decoded duration within one second of metadata before final frame padding. Only a verified final partial frame may be padded once within the byte/sample limits. Short reads, low buffers and exit zero alone never prove completion. _(M5)_
- **PLAY-11** A catalog source is ready at six seconds of reserved PCM, or, for a shorter positive-duration track, only after all strict completion checks pass. Production completion and final audible drain are distinct. A late failure after a streamed prefix was heard remains a failed track and recovers within the bounded catalog policy; it is never reported as full completion. _(M5)_
- **PLAY-12** Catalog selection, retry, prefetch and no-repeat history obey the [fixed catalog bounds](architecture.md#catalog-selection-and-failure-bounds). Keep at most the audible catalog track and one prepared next track; exhausted searches do not loop without backoff or clear history to repeat tracks. _(M5)_

PLAY-9 explicitly supersedes the catalog track-to-track and cross-mode crossfades
in the original [M5 outcome and exit criteria 2/4](https://github.com/slackysoba/sobafm/issues/114)
and #117's original sequential-ND-fade recommendation, as accepted in
[ADR-0006](decisions/0006-play-catalog-and-generated-music-in-one-station.md#explicit-reconciliation-of-original-crossfade-criteria).
Those historical criteria remain preserved with their dispositions; they are not
observed passes. NFR-2/NFR-3, supported platforms, generated behavior and every
other unaffected criterion retain their limits.

## Interpretation

- **AI-1** Gemini turns each accepted request into a validated mode-specific plan: a generated `MusicPlan` with title, one to four weighted style prompts and optional tempo, key, density, brightness, drum muting and wordless vocals, or a catalog query with descriptive search terms and explicit requested attributes. Relative requests receive only the same-mode current plan; cross-mode requests start fresh, with this limitation explained. _(M3; M5)_
- **AI-2** Model output and external catalog metadata are validated before they change state or trigger search, fetch or playback. _(M3; M5)_
- **AI-3** Non-music requests, injection classifications and Gemini safety refusals produce a short refusal, preserve current music and trigger no music-source search or generation. _(M3; M5)_
- **AI-4** Generated interpretation keeps v1's raw-text fallback on error, timeout or invalid output: a lighter prompt added to the same-mode current plan, or the only prompt otherwise, with an explanatory reply. Catalog interpretation failure reports unavailability and preserves current music; it never searches raw request text. _(M3; M5)_
- **AI-5** Names of artists, songs and other works are translated into descriptive style terms for either mode. _(M3; M5)_
- **AI-6** Only request text and the same-mode current plan go to Gemini; descriptive validated search terms/attributes and the operator's client id go to Jamendo. No Discord identifiers go to either provider. _(M3; M5)_
- **AI-7** Separate generated and catalog evaluation sets measure interpretation quality on demand with an API key, outside CI. Retain #116's qualitative acceptance and weak-fit follow-through without inventing ratings or a numerical relevance pass. _(M3; M5)_

## Settings

Settings are stored per server and persist across restarts. _(M3)_

| ID | Setting | Default | Range and scope | Milestone |
| --- | --- | --- | --- | --- |
| SET-1 | Play duration | 60 minutes | 5 to 240 minutes; generated programs only | M3; M5 |
| SET-2 | Volume | 50% | 1% to 100%, as linear gain; 100% leaves the level unchanged; shared by both modes | M3; M5 |
| SET-3 | Change cooldown | 30 seconds | 0 to 600 seconds, server-wide across `/play`, `/freestyle` and `/skip`, checked before interpretation/selection; only its owning failed request may release it | M3; M5 |

## Feedback

- **FB-1** Initial start/replacement replies describe the source actually accepted by a connected player: program title, interpreted direction, requester, generated end time or indefinite catalog mode, and the first catalog track's credit. Keep generated slow-start behavior. No automatic per-track chat posts are introduced, and initial replies are not the credit updater for later tracks. _(M3; M5)_
- **FB-2** When permitted, per-voice-channel status shows the generated title or heard catalog song/artist, shortened within 500 characters. Preserve ordered best-effort updates/retries and reconnect/move/empty-channel clearing. Missing, stale, overwritten or failed status neither substitutes for full private credits nor stops playback; add no mandatory status or Manage Channels permission. _(M3; M5)_
- **FB-3** Refusals, invalid credentials, exhausted quotas/rate limits, no eligible match and outages produce specific safe feedback. Preserve music already heard where possible; a replacement that cannot start restores that program if still viable, including a generated program's original deadline. BUSY before generated admission leaves catalog music intact. _(M3; M5)_
- **FB-4** Model and third-party text is escaped, stripped of bidirectional controls and sent with mentions/previews disabled. Only validated credential-free application-rendered links remain usable. Shorten summaries where needed; required full credits follow FB-5/FB-6 rather than silent truncation. _(M3; M5)_
- **FB-5** Before catalog admission, build an immutable source-bound credit snapshot with title, supplied creators/designated attribution parties, exact license name/version/URI, Jamendo provider credit, direct validated track backlink, and all required supplied copyright, license, disclaimer notices and associated licensor links. Missing/conflicting required information or unsafe/incomplete rendering makes the candidate ineligible. Notice completeness requires actual-source qualification; absent API fields do not prove no notice is required. _(M5)_
- **FB-6** Complete private `/now` credits fit 32 KiB raw UTF-8 and 16,000 escaped UTF-16 code units, rendered in at most ten pages of 1,900 UTF-16 units each including labels with intact links. Exceeding a bound rejects the candidate. Caller-bound previous/next buttons edit the private response; label the heard song and snapshot time. Keep one view per caller/server, at most 16 per server/64 per process, expiring or evicting the oldest within ten minutes and releasing all on station shutdown. Expired views direct callers to invoke `/now` again. _(M5)_
- **FB-7** Track credits/history change only at connected-player audible-source settlement, including consecutive tracks of one program. Pending/failed replacements keep heard-source credits; stale/cancelled/disconnected callbacks cannot announce the next track. Private interaction responses are acknowledged/deferred within three seconds, follow rate limits, and never retry beyond their view lifetime or fall back publicly. No new Send Messages permission is required. _(M5)_

## Operation

- **OPS-1** Operators configure SobaFM through environment variables or a `.env` file; `DISCORD_TOKEN` and `GEMINI_API_KEY` are required. Optional per-operator runtime `SOBAFM_JAMENDO_CLIENT_ID` enables catalog configuration and is held as a secret. The standalone probe retains its historical `JAMENDO_CLIENT_ID` spelling. _(M1; M5)_
- **OPS-2** SobaFM uses only the Guilds and Guild Voice States gateway intents. _(M2)_
- **OPS-3** SobaFM runs from source with uv. _(M2)_
- **OPS-4** SobaFM runs as a multi-architecture container image with a persistent data volume. _(M4)_
- **OPS-5** Logs go to standard output. Secrets, client ids, audio URLs/query strings, raw provider/native errors, interaction tokens and interaction objects never enter logs or persistent state; request text appears only at debug level. Use fixed/allowlisted failure categories, status codes and sanitized identities. _(M2; M5)_
- **OPS-6** Catalog decoding uses the verified minimal shared FFmpeg LGPL-2.1-or-later route in ADR-0007, with GPL/nonfree/version3/network disabled. Before distribution, bind every runtime/package/image to exact source/build/artifact identities, complete component inventory/SBOM/notices and corresponding source/build/install materials, durable direct source access, and independently verified recipient rebuild/shared-library replacement. Arbitrary PATH binaries are ineligible. _(M5)_
- **OPS-7** The release maintainer owns native source/notices availability while binaries remain obtainable, monthly and upstream-notice advisory review, applicable-fix triage within two working days, and refreshed identities/platform checks for updates. Suspend catalog decoding for an applicable serious unmitigated defect. Python dependency scanning alone is not native coverage. _(M5)_
- **OPS-8** Catalog runtime readiness and shipping require qualified supply identity, compiler-free supported installations, pre-input containment, shared accounting, completion/cleanup and listener credits/access. Missing prerequisites keep catalog unavailable with actionable guidance while generated availability retains its behavior. #174 supplies native packages; #175 qualifies implementation. These gates follow implementation rather than circularly blocking it. _(M5)_

## Non-functional requirements

- **NFR-1 Latency.** Generated playback retains the 15-second median from request to audible music, now through `/freestyle`, with historical evidence/limits preserved. _(M3; M5)_
- **NFR-2 Endurance.** A program plays for 60 minutes with at most 2 seconds of total underrun under normal service conditions. _(M2)_
- **NFR-3 Footprint.** All attributable resident audio storage for a playing server stays strictly below 40,000,000 bytes, including every retained source, encoded input, PCM/native/transport buffers, copies, overshoot and cleanup debt across mode changes. Process RSS, producer counts and nominal buffer arithmetic do not prove this bound. _(M2; M5)_
- **NFR-4 Platforms.** SobaFM runs on Linux x86-64, native Linux ARM64 and Windows x86-64, from source and the applicable container images, without a compiler on the operator's machine. Emulation is not native qualification. _(M4; M5)_
- **NFR-5 Catalog latency.** Under normal service conditions, admitted `/play` requests reach their first audible catalog frame within a median of 15 seconds; measure admitted `/skip` separately to the first audible replacement frame against the same median. Access/cooldown rejections are not admitted requests; report admitted failures/timeouts separately. These new targets are unmeasured and require #131 verification, distinct from metadata, first-PCM and slow-start deadlines. _(M5)_
- **NFR-6 Audio admission.** Enforce one leased station byte ledger: aggregate PCM at most 34,560,000 bytes; parent audio at most 38,000,000 bytes; parent plus audited native audio strictly below NFR-3. Reserve allocations/copies/in-flight overshoot and reachable future Lyria growth before catalog admission. Aliases count once and copies separately. Preserve Lyria generation/spare rules; no fixed retained-source count or guaranteed decoder headroom is inferred. Unqualified or infeasible bounds keep catalog unavailable pending reviewed resolution. _(M5)_
- **NFR-7 Decoder bounds.** Catalog producers obey the [decoder guardrails](architecture.md#decoder-guardrails): incremental MP3 input, hard byte/sample/queue/time limits, at most two children per server/four per process, and 128 MiB child containment established before untrusted input. HTTP, decoding, pipe I/O, waiting and cleanup never block the mixer; native decoding stays off the event loop. _(M5)_
- **NFR-8 Cleanup.** One cleanup owner has a single monotonic three-second total budget for feed cancellation, stdin closure, all task joins, concurrent bounded pipe drain/closure and process-tree terminate/kill/reap. Repeated cancellation cannot interrupt it or reset the deadline. Unfinished cleanup is failure and retains its leases/debt/child slot across station replacement until actual release, blocking reuse of that capacity. _(M5)_

## Responsible use

- **USE-1** Before its setup instructions, the README states that the Gemini API terms require users to be 18 or older and restrict use in services likely to be accessed by minors, that users in the EEA, Switzerland, and the UK require a billing-enabled project, and that Google may review prompts sent on the free tier. _(M4)_
- **USE-2** Command/help/setup descriptions explain requests sent to Google for either mode and descriptive catalog searches sent to Jamendo, without Discord identifiers. _(M3; M5)_
- **USE-3** The project does not operate a public instance; each operator is responsible for their own deployment. _(M4)_
- **USE-4** Catalog use complies with Jamendo's per-application registration, protected credentials, non-commercial free-API terms, privacy-policy and creator/provider-credit obligations. A commercially usable individual track does not authorize commercial free-API use, advertising, affiliate revenue or paid access. Operators remain responsible for deployment eligibility and current terms. _(M5)_
- **USE-5** Only exact reviewed CC BY, BY-NC, BY-ND, BY-NC-ND, BY-SA and BY-NC-SA **3.0 Unported** families are eligible, with the actual track's rights and complete required notices. Unknown versions, jurisdictional ports, conflicting metadata and known unavailable required notices reject a candidate. A generic API license flag, transport encryption or collection membership is not blanket permission; resolve concrete rights conflicts before use. _(M5)_
- **USE-6** Catalog audio uses transient bounded in-memory streaming buffers only, with no download/offline feature, audio persistence or replay cache. Retain no credential-bearing URL in child arguments/environment; preserve accepted encrypted Discord transport and add no restriction denying listeners their CC rights or implying endorsement. _(M5)_

## Out of scope

- **SCOPE-1** Voice input; queues and playlists; generated vocals with lyrics; Stage channels; more than one voice channel per server; a hosted public instance; a web dashboard; other catalogs; catalog audio downloads/offline playback and expressive editing remain out of scope. Catalog lyrics are an M5 target under PLAY-8; v1's instrumental constraint remains for generated music. _(M5)_

## Open questions

| Question | Needed by | Tracking |
| --- | --- | --- |
| Lyria RealTime's session limit, generation rate, concurrency, and API version | ADR-0004 | Answered in #11 |
| Whether continuous playback is achievable at the measured generation rate, and the policy if it is not | M2 | Answered in ADR-0004 (#12): achievable with two sessions per playing server |
