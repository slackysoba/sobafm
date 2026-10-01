# ADR-0004: Stream Lyria RealTime through buffered decks and a crossfading mixer

- **Status:** Accepted
- **Date:** 2026-10-01
- **Issue:** #12

## Context

A program must play for an hour with at most 2 seconds of underrun, change with a crossfade, and start within the latency target ([PLAY-1, PLAY-3, NFR-1 to NFR-3](../requirements.md)). The measurements in #11 found:

- **Session limit.** The server closes every session after 600 seconds, with WebSocket code 1011 and no warning (3 of 3 runs).
- **Generation rate.** Generation averages 0.97× to 0.99× real time over a session. Per-minute rates range from 0.77× to 1.03×, and one chunk arrived 17 seconds after the previous one.
- **Start latency.** Audio starts a median 2.9 seconds after `play()`, and at most 10.9 seconds.
- **Flow control.** Sessions survive pauses of up to 300 seconds and resume in under a second.
- **Concurrency.** Four concurrent sessions played normally when started 45 seconds apart. A third session started 10 seconds after the second received a `filtered_prompt` and no audio.
- **Format and API.** Chunks are always 2 seconds and frame-aligned. API versions `v1beta` and `v1alpha` both work.

## Requirements

1. At most 2 seconds of total underrun per hour under normal service conditions (NFR-2).
2. Plan changes heard within the latency target, through a crossfade (NFR-1, PLAY-1).
3. No audible gap at the session limit or when a session closes unexpectedly (PLAY-3).
4. Memory below 40 MB per playing server, and bounded Lyria usage (NFR-3).

## Options considered

1. **Buffered decks with two sessions per playing server.** The live deck plays while the next deck fills ahead. Handovers crossfade to the next deck at the session limit, when the live deck runs low, or when the plan changes. Deep buffers absorb stalls. Each playing server uses two concurrent sessions.
2. **One session per server, rotated with a short fade gap.** Uses half the sessions, but every rotation leaves a gap of 3 to 11 seconds while the new session starts, and stalls of up to 17 seconds drain a short buffer. This fails requirements 1 and 3.
3. **One session with a deep initial buffer.** Buffering 20 to 30 seconds before playing would absorb stalls, but it breaks the latency target, and the session limit still forces a gap at every rotation.

## Decision

Option 1, with these rules and constants:

- **Sessions per server.** Each playing server keeps at most two sessions: the live deck's and the next deck's. A session is retired after 540 seconds, or when its plan is replaced. A retired deck's buffered audio keeps playing.
- **Buffers.** The next deck fills until it holds 60 seconds, then pauses. The live deck pauses at 60 seconds and resumes below 45 seconds.
- **Pre-roll.** A deck may go live once it holds 6 seconds.
- **Handover.** The station hands over with a 4-second equal-power crossfade, clamped to the outgoing deck's remaining audio. It hands over when:
  - the live session reaches its limit or closes;
  - the live deck holds less than 4 seconds and the next deck is ready; or
  - the plan changes. The next deck is replaced by one for the new plan, which goes live at its pre-roll.
- **Fades.** 2 seconds in and 3 seconds out.
- **Refused sessions.** A `filtered_prompt` on a session that has produced no audio is retried once after 30 seconds. A second refusal is reported to the requester as a filtered prompt.
- **Global cap.** At most four concurrent sessions per process, configurable with `SOBAFM_MAX_SESSIONS`. This allows two playing servers per API key; further requests receive a message that SobaFM is busy.
- **API version.** SobaFM uses the documented `v1beta`; the version is not configurable.

The constants live in code as named values, next to the deck and station logic that uses them.

## Consequences

- Each handover is a crossfade between two independently generated takes of the same plan, about every 9 minutes. Musical continuity holds within a take but not across a handover.
- During a handover a playing server holds up to three buffers, about 35 MB (NFR-3).
- At the start of a program, the live deck holds only its pre-roll until the next deck is ready. An early stall therefore triggers an immediate handover, or a brief underrun if the next deck is not ready yet.
- The rate limit on session starts is not documented. A real content filter is reported after one retry, so a filtered request is reported about 30 seconds late.
- The measurements come from one API key on one day. `scripts/lyria_probe.py` repeats them when the SDK or the model changes.

## Revisit triggers

- Lyria RealTime publishes a session limit, session resumption, or advance warning for music, or the 600-second limit changes.
- Generation falls below 0.95× over a session, or stalls outlast the next deck's buffer.
- Session starts are refused at the rates SobaFM uses, or quota errors appear.
- A generally available real-time music model replaces `lyria-realtime-exp`.
