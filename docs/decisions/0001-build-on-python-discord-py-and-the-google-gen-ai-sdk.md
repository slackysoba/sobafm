# ADR-0001: Build on Python 3.14, discord.py 2.7, and the Google Gen AI SDK

- **Status:** Accepted
- **Date:** 2026-10-01
- **Issue:** #5

## Context

SobaFM needs Discord slash commands and voice playback, a streaming connection to Lyria RealTime, and Gemini calls with structured output. Since March 1, 2026, Discord accepts only voice connections that support DAVE end-to-end encryption ([change log](https://docs.discord.com/developers/change-log)), which narrows the choice of Discord library. Lyria RealTime streams 16-bit, 48 kHz stereo PCM ([guide](https://ai.google.dev/gemini-api/docs/realtime-music-generation)), the format Discord voice libraries encode to Opus, so no stack needs to transcode. SobaFM is self-hosted, so installation must be simple on Linux (x86-64 and ARM64) and Windows.

## Requirements

1. DAVE voice support and handling of Discord's current voice close codes in a released library version.
2. Streaming of raw PCM from an asynchronous source, with defined underrun behavior.
3. First-party SDK support for Lyria RealTime and Gemini structured output.
4. Installation without a compiler on the target platforms.
5. Active maintenance and permissive licenses.

## Options considered

1. **Python with discord.py and the Google Gen AI SDK.** discord.py added DAVE in 2.7.0 (February 2026) through the `davey` library, and its released voice client follows Discord's guidance for each voice close code. Audio sources are pull-based: discord.py's player thread calls `read()` every 20 ms, so returning silence covers underruns. The Google Gen AI SDK's asynchronous `live.music` client yields audio as bytes, and Google's Lyria RealTime cookbook is written in Python. Every dependency ships wheels for x86-64, ARM64, and Windows; Linux hosts also need the system Opus library. Drawbacks: the player thread needs a thread-safe hand-off from asyncio, and the `voice` extra pins PyNaCl below 1.6.
2. **TypeScript with discord.js, @discordjs/voice, and @google/genai.** @discordjs/voice added DAVE in 0.19.0 (August 2025), and its streams are push-based. It requires Node 22.12 or later, while its next major release requires Node 24, for which `@discordjs/opus` publishes no prebuilt binaries. Its released version rejoins after close codes that Discord says should not be retried; the fix is unreleased. The JavaScript SDK also places the API key in the WebSocket URL, which complicates keeping secrets out of logs.
3. **Another Python Discord library (py-cord, nextcord, or disnake).** Each supports DAVE, but added it later than discord.py, has a smaller ecosystem, and offers no advantage for this use.

## Decision

Option 1: Python 3.14 with `discord.py[voice]` 2.7, `google-genai`, `pydantic` and `pydantic-settings` for validation and configuration, and `audioop-lts` for gain and mixing. uv manages environments and the lockfile. The SobaFM-specific surface is the playback engine (decks, mixer, and station) and the interpreter, described in the [architecture](../architecture.md).

## Consequences

- Lyria audio reaches Discord without transcoding, and underruns degrade to silence instead of ending playback.
- Audio crosses from the event loop to discord.py's player thread through thread-safe frame buffers, and `read()` must never block ([threads and buffers](../architecture.md#threads-and-buffers)).
- discord.py 2.7 pins PyNaCl to 1.5, whose bundled libsodium is affected by CVE-2025-69277, a flaw in `crypto_core_ed25519_is_valid_point` (CVSS 4.5). Voice transport encryption is not expected to call that function; #9 confirms reachability and adds a time-limited, documented exception to the dependency scan until discord.py allows PyNaCl 1.6.2 or later.
- Linux hosts and container images need `libopus`.
- discord.py's most recent release was in March 2026, and fixes on its development branch are not yet released.

## Revisit triggers

- discord.py stops releasing while Discord's voice protocol changes.
- A Lyria RealTime capability ships in another SDK first.
- The PyNaCl exception reaches its expiry with the pin still in place.
