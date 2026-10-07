# Roadmap

- **Status:** Approved (#5); M5 added in #115
- **Related:** [Requirements](requirements.md) · [Architecture](architecture.md) · [SobaFM project](https://github.com/users/slackysoba/projects/2)

SobaFM is planned in rolling waves. Every milestone has an outcome and exit criteria, but only the current and next slices are broken into issues; later work is detailed once earlier milestones settle the design. Execution state lives in the [project](https://github.com/users/slackysoba/projects/2).

| Milestone | Outcome | Depends on | Issue |
| --- | --- | --- | --- |
| M1 Project foundation | Approved architecture, contributor policies, Python toolchain, CI, and a protected `main` branch | None | #1 |
| M2 Continuous playback | A raw-text request plays continuously for an hour across Lyria RealTime session rotations | M1 toolchain | #2 |
| M3 Requests to music | Gemini interprets requests; duration, volume, cooldown, and feedback complete the experience | M2 | #3 |
| M4 Release 1.0 | A published container image and a self-hosting guide anyone can follow | M3 | #4 |
| M5 Catalog playback | `/play` plays openly licensed Jamendo tracks that match a request; generated music moves to `/freestyle` | M4 | #114 |

## Milestone capabilities

**M1 Project foundation.** Requirements, architecture, and decision records; community health files and agent instructions; the Python toolchain and package skeleton; CI and security workflows; repository settings and the `main` ruleset.

**M2 Continuous playback.** A measurement of Lyria RealTime's limits (#11) and the playback engine decision (#12); `/join` and `/leave`; decks, the mixer, and the station reconcile loop; raw-text `/play` and `/stop`; the empty-channel rule; a one-hour soak test.

**M3 Requests to music.** The interpreter and its evaluation set; interpreted `/play`, including relative requests; the duration timer; `/now`, the now-playing message, and the voice channel status; error messages; `/settings` and the change cooldown.

**M4 Release 1.0.** The container image and compose file; the release workflow; the self-hosting guide; the README demo and responsible-use notes; `v1.0.0`.

**M5 Catalog playback.** A probe of how well Jamendo search matches requests (#116); decisions on playing catalog and generated music in one bot (#117) and on decoding catalog audio (#118); a station with one engine per music source; a Jamendo client, track decks, and catalog interpretation with an evaluation set; `/play` with attribution for each track, `/freestyle`, and `/skip`; the README and self-hosting guide; a soak test; and a release.

## Sequencing notes

- The Lyria RealTime measurement (#11) runs alongside M1, because its results decide whether continuous playback is achievable and set the playback engine's constants.
- Deck and mixer work waits for ADR-0004 (#12); `/join` and `/leave` (#13) need only the toolchain.
- If continuous playback is not achievable at the measured generation rate, ADR-0004 records the policy before M2 continues.
- M5 starts once M4 is ready to ship, and replaces #40's plan for a separate Jamendo bot. Its issues are created ahead of the rolling wave, so the plan is recorded before work starts. The implementation issues stay in Backlog until #117 and #118 are accepted. If the probe (#116) fails, M5 ends there.

## Later ideas

These are draft items in the project and are not committed: requests made by mentioning SobaFM in chat, blending several members' requests into one program, and Stage channel support.
