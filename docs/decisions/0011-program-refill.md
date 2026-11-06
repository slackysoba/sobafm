# 0011: Refill a program's song list as it plays, 8 songs at a time

- Status: Accepted
- Date: 2026-11-06
- Scope: [issue #129](https://github.com/slackysoba/sobafm/issues/129)
- Authority: maintainer decision recorded on that issue. They proposed requesting songs in batches and, asked how many songs per request, answered "Let's do 8 at a time". That covers the bot making these calls while a program plays, with nobody typing a command. It doesn't approve any call during development.
- Supersedes: in [decision 0006](0006-gemini-play-matching.md), "one call per `/play`" and the 20-song limit. The rest of that record stands: best fit disclosed, authority staying local, what leaves the service, failure preserving audio, the API and its dependency.

## Problem

The requirements call a `/play` program indefinite, but decision 0006 ends it after one call's list. The [live session](https://github.com/slackysoba/sobafm/issues/124#issuecomment-6677813509) saw lists of 10, 1, 20 and 11. The list of one left the station silent after one song, and the maintainer reported that as "the next song didn't play".

## Options

1. **Keep one call and ask for a longer list.** The first reply would come slower and cost more, and the output limit would have to grow. The model chooses the length anyway, so a list of one could still happen.
2. **Fill the rest locally once the list runs out.** This costs nothing, but the later songs would have nothing to do with the request.
3. **Ask for more in batches as the list runs out.** Each call is smaller than the last, and music plays while it runs. A program makes more calls over its life, with nobody typing a command.
4. **Leave it as it is.** A radio station that can go silent after one song.

## Decision

Use option 3.

- **Eight songs per call, best first**, including the first call from `/play`. The instruction allows fewer only when fewer are listed. A shorter reply is still accepted, because rejecting it would fail a usable answer, and the next call simply comes sooner.
- **When the last song on the list starts**, the bot asks for 8 more in the background. The list it sends leaves out everything the program has played or tried, and numbers what remains, so the model can only name an unplayed song. The reply replaces the program's list.
- **Music keeps playing while it waits.** The call doesn't wait for a free audio bank, and the next track then prepares as usual and follows the current one.
- **Three calls per refill, about 15 and then 60 seconds apart.** A failed call, a refusal and a list with nothing new each use up one call. They take the same path because a refusal is a verdict on a request that an earlier call accepted, so it's more likely variation than a judgment worth ending on. The waits are why none of them can become a hot loop. The longer second wait lets the last call outlast a per-minute rate limit, the likely cause of fast failures, rather than spending all three calls inside the same minute.
- **The track sets how long the second wait can be.** It is capped to leave 16 seconds before the heard track ends: the last call's 10-second timeout and six seconds of pre-roll. No wait is ever shorter than 15 seconds, which is also what applies when nothing is audible or a recording has no listed length. So a short track behaves as it did with fixed 15-second waits, or calls later and still fits. Time left is the recording's catalog duration minus the frames handed to the voice player. It is only a timing hint: a duration that's too long can leave a gap of silence before the last call, and nothing else depends on it.
- **An honest ending.** After the third call, the current song finishes and the program ends with `reason=refill_failed`. `/now` and `/skip` say Gemini did not supply more recordings. If nothing is audible by then, it ends at once.
- **Cancellation.** A `/play` accepted for a new program, `/stop`, `/leave`, listener-grace expiry and shutdown cancel a refill, and so does losing voice. A result that still arrives changes nothing, because it is checked against the desired program before it is used. If a replacement then fails, the old program is desired again and asks afresh.
- **`/skip` never waits on the model.** If nothing is ready to skip to, the existing wording applies at once ("The next recording could not be prepared; the current recording continues") and the skip gives back its cooldown.
- **Every admitted song played or tried** ends the program with `reason=exhausted`. Nothing repeats automatically, and no call is made when nothing is left to send.
- **The reply's fit disclosure stays on the first song.** Later songs may fit less well, because the best matches go first.
- **A refill isn't a member action**, so it takes no change cooldown and frees none.

**Bounds.**

- At most one refill per program is in flight, inside the existing limit of four calls across all servers. The 10-second timeout and the response limits don't change.
- A program makes a finite number of calls. For the current 167 recordings that is about 21: one for each 8 songs. Each refill that succeeds adds at least one unplayed song, so even a model returning one song at a time makes at most one successful call per admitted recording. With retries, no program can exceed three calls per admitted recording.
- The worst refill takes 105 seconds: three 10-second timeouts and both waits. The cap keeps it inside the track: with six seconds of pre-roll, the last call fits every admitted recording, including the shortest, now 74 seconds, whether calls fail at once or time out. A test checks this against every duration in the reviewed catalog.
- The list is replaced rather than extended, so plan memory holds at most 8 references. History stays bounded by the admitted catalog. A smaller song list is rendered only while its call holds a slot, as the request body already is. No capacity partition changes.

| Heard recording | When the calls start, if each fails at once |
| --- | --- |
| 665 seconds, the longest | 0, 15 and 75 seconds |
| 227 seconds, about the mean | 0, 15 and 75 seconds |
| 74 seconds, the shortest | 0, 15 and 58 seconds |
| Nothing audible, or no listed length | 0, 15 and 30 seconds |

Fast failures span more than a minute on all but the three shortest recordings in the current catalog, which last 74 to 76 seconds.

## Cost and data use

These are estimates, not measurements. Prices are from Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing), checked on 2026-11-06, for `gemini-3.5-flash-lite` on the paid tier: $0.30 per million input tokens and $2.50 per million output tokens, including thinking tokens. The token counts assume 3 to 4 characters per token over the rendered list, and no call was made to measure them.

| Program | Calls | Input tokens | Estimated cost |
| --- | --- | --- | --- |
| One song, then replaced | 1 | 23,000 to 30,000 | About 1 US cent |
| An hour of music, about 16 songs | 3 | 65,000 to 85,000 | About 2 to 4 US cents |
| The whole catalog, about 10.5 hours | 21 | 250,000 to 330,000 | About 8 to 21 US cents |

Output accounts for most of the range in the last row: under one cent if each reply is about 120 tokens, about 11 cents if every call reached the 2,048-token cap. Each retry adds at most one more call of that size. The first call is the largest, because later lists are shorter. A larger catalog costs more, up to the song list's limit of 512,000 characters.

**Data use.** What leaves the service is what decision 0006 lists, now sent again as a program plays: the request text, a fixed instruction, and the title, creators, length, labels and details of each recording not yet played. It has no Discord identities, credits or credentials, and `store` stays off. Google's pricing page lists "Used to improve our products" as yes on the free tier and no on the paid tier. So on the free tier, each refill's content may be used to improve Google's products, and that applies while a program plays with nobody typing. Choosing the free tier costs nothing, but it also has rate limits, and a refill that hits one counts as a failed call.

## Consequences

- A program can now run for the length of the catalog, making calls with nobody typing. The listener grace period stops it when no one is listening.
- A provider outage at the wrong moment ends a program that used to end anyway, with a more specific explanation.
- A recording shorter than 66 seconds could leave a gap of silence rather than fail, because a wait never drops below 15 seconds. If a catalog ever admits one, the test above fails and the bounds need another look.
- A silent program whose refill is running keeps its priority for a bank, as any silent program does, even while a bank is free. So while server A is silent with a refill running, server B's next track waits, and so does B's `/skip`: after its five-second wait the reply says it is skipping, and the switch can't come before A's refill settles. That is up to 60 seconds, or about 70 if A's recording ended before its listed duration. Without this priority, B's tracks could hold both banks and A's program could end as unavailable.
- Replies don't warn that later songs may fit less well. [Operations](../operations.md) says so.

## Verification needed

Offline tests establish the schedules: a refill at the last song and on a list of one, played songs left out, the retry bound, spacing and ending, refusals and empty results, every kind of cancellation with stale results discarded, `/skip` during a refill, exhaustion and the total call bound. They don't establish that the live model returns 8 songs when asked, how well later batches fit, or the token counts above. A short live check with the maintainer's go-ahead comes before Outcome 2 closes.
