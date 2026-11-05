# 0007: A catalog from the FMA research dataset, admitted in one batch

- Status: Accepted
- Date: 2026-11-05
- Scope: [issue #122](https://github.com/slackysoba/sobafm/issues/122)
- Authority: the maintainer's answers "FMA dataset" on [#119](https://github.com/slackysoba/sobafm/issues/119#issuecomment-6656728152) and "Accept all" on [#122](https://github.com/slackysoba/sobafm/issues/122#issuecomment-6657845523), each after the points below were stated
- Changes how `credits_complete` and `access_ready` are established for this catalog. The requirement in [decision 0002](0002-prompt-catalog-selection.md) that a separate trusted review bind them to each recording's exact digest stands.

## Problem

Best-fit matching ([decision 0006](0006-gemini-play-matching.md)) needs a real catalog, and the only real one held one song, admitted for one private test. [ccMixter refuses non-browser clients](https://github.com/slackysoba/sobafm/issues/111#issuecomment-6654375637). Free Music Archive's API is closed and its site can't be scraped. Jamendo [needs streaming code](https://github.com/slackysoba/sobafm/issues/119), and its route is untested.

Each admission review asserts complete credits and access readiness for one recording. Reviewing about 200 one at a time would take many hours, and the requirements don't let metadata or a model approve itself.

## Options

For the source:

1. **The FMA research dataset ([mdeff/fma](https://github.com/mdeff/fma)).** Read over ordinary range requests from its own host, with no signup. Local files, so no streaming code.
2. **Jamendo.** Needs a client ID, new playback code and a probe that hasn't been able to run.
3. **FMA by hand.** The maintainer's own downloads, with credits gathered by hand. It would stay small.

For admission:

1. **The maintainer accepts a written rule and the resulting list.** A deterministic script applies the rule, and the acceptance is the trusted review.
2. **The maintainer reviews each recording.** More scrutiny per recording, and far more time.
3. **An agent reviews and approves.** Ruled out: the requirements don't allow it.

## Decision

Use the dataset, and admit by rule.

**The rule, version `fma-dataset-1`.** It lives in `src/sobafm/catalog_prepare.py` and has no model in it. A track qualifies only if it has a Creative Commons license link, a usable FMA track page, a length of 1–15 minutes, at least 128 kbps, a predicted size of at most 20 MB, at least 1,000 listens and a listed genre. It must not be flagged Radio-Unsafe or Adults-Only, be part of a radio program, be listed as spoken word, poetry, interview, radio, comedy, sound effects or field recordings, or name a sample, stem, preview, excerpt or interview in its title. Its title, artist and license must be reproducible in full.

**Selection.** Genres take turns from most to least listened, each picking its most listened remaining track, with at most two tracks per artist and one per artist and title. The maintainer struck three tracks with vulgar titles from the list, and two more from the final list because their own listings point at someone else's rights. One names a composer whose work is copyright controlled in some countries, and the other's artist says some of their music has uncleared samples.

**Licenses.** Any Creative Commons license FMA lists: BY, BY-NC, BY-SA, BY-NC-SA, BY-ND and BY-NC-ND. The bot plays each recording unmodified, with attribution and no monetization. This is a reading of the license text, not legal advice.

**Validation.** Before admission, offline, in a container at the [ADR 0004](0004-amd64-whole-service-adoption.md) limits: the archive's CRC-32 and size, the real decode path exiting zero, a decoded length within 2 seconds of the listing, no silence over 30 seconds or sustained full-scale clipping, and embedded tags that don't contradict the listed license or page. Anything that fails is dropped with its reason, and nothing is fixed by hand.

**What the review is.** The maintainer reads the rule, sees the final list with every rejection and its reason, and spot-checks ten recordings chosen from a hash of the exact catalog. Only after that does the tool write `reviews.json`. Each review links to the issue and names the rule version. No agent approves anything.

**Storage and requests.**

| Item | Decision |
| --- | --- |
| Location | `./data/fma/` on the operator's machine, mounted read-only at `/data`; never in Git or the image |
| Limits | At most 256 recordings, 20 MB per file and 3 GB in total; MP3 only |
| Requests | The dataset's host only, one file at a time, at least two seconds apart, ordinary range requests with no changed headers or identity, stopping at the first refusal |
| Removal | On a takedown, rights conflict or failed check, delete the review line and the file, then restart |
| Backup | None; the metadata is in Git, and the audio can be fetched again while the dataset's host offers it |

**Rechecks.** None happen. The requirements ask for availability and rights to be rechecked, and this snapshot can't do that, so the maintainer accepted it for now, with the takedown path above. Reading FMA's public pages stays out unless they approve it.

## What a review can't establish

The acceptance is a decision to go ahead despite these, not evidence about them.

- **Research purposes.** The dataset says it "is meant for research purposes", and a radio bot isn't research. It distributes the audio "under the license chosen by the artist". No other terms were found.
- **A third-party copy.** FMA's [policy](https://freemusicarchive.org/app-developers) allows its music "provided you host it on your own server" under the license conditions, and doesn't address copies from someone else. The maintainer's direct permission covers FMA's music in SobaFM. Whether that reaches a research copy can't be shown by a review.
- **Nothing is known after the snapshot.** Listed tracks date from 2008 to 2017. A license change, a takedown or an artist's change of heart since then is invisible here.
- **Sample use isn't recorded per track.** An artist's notes sometimes mention samples, but nothing lists what a given track samples.
- **Compositions.** A performer's license covers their recording, not a composition they don't control. The rule doesn't look for this. A keyword search of the listing text found what the listings say outright, and unlabeled covers stay invisible.
- **Explicit content isn't reliably flagged.** The flag is blank for about 97% of tracks, so lyrics nobody has read may be explicit.
- **How it sounds.** Nobody listened. Selection read tags and listen counts only.

## Alternatives and consequences

- Jamendo stays parked. If a real need appears, [#119](https://github.com/slackysoba/sobafm/issues/119) holds the plan, including the streaming risks.
- The dataset's Night Owl is cut short, so it stays out. The maintainer's own copy stays separate and isn't part of this catalog.
- The dataset holds some cut-short files and exact duplicates. Validation and the one-per-artist-and-title rule handle those. Others could still get through as the same recording under different names.
- Listen counts favor what FMA's audience liked up to 2017, which is a quality signal only in a loose sense.
- The dataset's metadata is CC BY 4.0, so the docs and each recording's credits name it.
- The catalog, reviews and manifest are metadata and live in Git. Audio doesn't. Using them on another machine means fetching the files again, which needs that operator's own decision.
- A rebuild changes digests wherever reviewed metadata changed, so those recordings need a fresh acceptance. Changing the `details` alone doesn't, because they stay outside the digest.

## Verification needed

Offline tests establish the rule, the request limits and the stop conditions against a loopback fake. The real run's results are on the issue: request counts, validation outcomes, the startup check and peak memory at the service limits. None of that establishes music quality, availability over time or Discord behavior.
