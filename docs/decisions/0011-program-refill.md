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
- **Three calls per refill, 15 seconds apart.** A failed call, a refusal and a list with nothing new each use up one call. They take the same path because a refusal is a verdict on a request that an earlier call accepted, so it's more likely variation than a judgment worth ending on. The wait between calls is why none of them can become a hot loop.
- **An honest ending.** After the third call, the current song finishes and the program ends with `reason=refill_failed`. `/now` and `/skip` say Gemini did not supply more recordings. If nothing is audible by then, it ends at once.
- **Cancellation.** A `/play` accepted for a new program, `/stop`, `/leave`, listener-grace expiry and shutdown cancel a refill, and so does losing voice. A result that still arrives changes nothing, because it is checked against the desired program before it is used. If a replacement then fails, the old program is desired again and asks afresh.
- **`/skip` never waits on the model.** If nothing is ready to skip to, the existing wording applies at once ("The next recording could not be prepared; the current recording continues") and the skip gives back its cooldown.
- **Every admitted song played or tried** ends the program with `reason=exhausted`. Nothing repeats automatically, and no call is made when nothing is left to send.
- **The reply's fit disclosure stays on the first song.** Later songs may fit less well, because the best matches go first.
- **A refill isn't a member action**, so it takes no change cooldown and frees none.

**Bounds.**

- At most one refill per program is in flight, inside the existing limit of four calls across all servers. The 10-second timeout and the response limits don't change.
- A program makes a finite number of calls. For the current 167 recordings that is about 21: one for each 8 songs. Each refill that succeeds adds at least one unplayed song, so even a model returning one song at a time makes at most one successful call per admitted recording. With retries, no program can exceed three calls per admitted recording.
- The worst refill takes 60 seconds: three 10-second timeouts and two 15-second waits. With six seconds of pre-roll that fits inside the shortest admitted recording, now 74 seconds, and a test checks this against the reviewed catalog.
- The list is replaced rather than extended, so plan memory holds at most 8 references. History stays bounded by the admitted catalog. A smaller song list is rendered only while its call holds a slot, as the request body already is. No capacity partition changes.

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
- Recordings shorter than the worst-case refill would leave a gap of silence rather than fail. If a catalog ever admits one, the test above fails and the bounds need another look.
- With no bank free, a silent program whose refill is running keeps its priority for a freed bank, so another server's next track can wait until it settles, at most 60 seconds.
- Replies don't warn that later songs may fit less well. [Operations](../operations.md) says so.

## Verification needed

Offline tests establish the schedules: a refill at the last song and on a list of one, played songs left out, the retry bound and ending, refusals and empty results, every kind of cancellation with stale results discarded, `/skip` during a refill, exhaustion and the total call bound. They don't establish that the live model returns 8 songs when asked, how well later batches fit, or the token counts above. A short live check with the maintainer's go-ahead comes before Outcome 2 closes.
