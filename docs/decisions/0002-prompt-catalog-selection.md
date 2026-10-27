# 0002: Prompt-focused selection across three sources

- Status: Accepted
- Date: 2026-10-27
- Scope: [issue #6](https://github.com/slackysoba/sobafm/issues/6)
- Authority: [maintainer direction](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441543704)

## Context

The maintainer asked to progress without requiring listening tests, prioritize
full-length or longer songs, prefer songs with more strong prompt matches, and
compare all three sources for a request. Jamendo, ccMixter and FMA already have
[confirmed direct permission](https://github.com/slackysoba/sobafm/issues/6#issuecomment-6441099250).
The earlier comparison proposal made human listening a gate for selection work.
There is no listening evidence to support an enjoyment or fidelity claim.

## Decision

Implement a small deterministic selector over prepared partitions for Jamendo,
ccMixter and FMA, pooling every supplied source before limiting results. Report
missing, partial, unavailable and empty coverage. Complete describes the dated
prepared subset, never the whole provider catalog or a live three-provider search.

Admit complete recordings only, with available metadata, immutable complete
credits and a separate trusted review bound to the exact recording metadata.
Reject previews, excerpts, stems, loops, sample packs and unknown completeness.
Do not pad short recordings; duration is not proof of completeness. Preserve
source identity, descriptor evidence, credit snapshots and rejection reasons.
Provider/model output cannot grant itself admission.

Apply explicit duration, identity and exclusion constraints before ranking.
Required descriptors need strong supporting evidence. Rank by most strong
supported preferences, fewest contradictions, most weak supported preferences,
fewest unknown preferences, longer known duration, then stable source/recording
identity. A long weak match cannot beat a shorter stronger match. Unknown is
never evidence of fit, and exact lookup never silently becomes a style search.

Provide a bounded typed query model and a small transparent phrase normalizer.
Report unsupported positive vocabulary; refuse unsupported hard constraints.
Use local JSON for a runnable demonstration that accepts prepared real metadata
and explicitly synthetic fixtures. Do not add live provider adapters, embeddings,
a general NLP framework or per-record model calls to this slice.

## Alternatives and consequences

Making the human comparison mandatory would conflict with the maintainer's
direction and delay usable selection behavior. Metadata ranking gives testable
ordering, not a subjective music-quality verdict. A larger model or audio-analysis
system would add cost and uncertainty without established need for this slice.

The earlier listening protocol and six-recording pilot are deferred optional
evidence. No listening approval is pending for this implementation, and there
is no two-person Discord audience limit. A later listening study requires its
own bounded scope; its panel size never limits the bot's ordinary audience.

Source-access rules, complete attribution, recording-specific eligibility,
storage restrictions, resource qualification and Linux targets are unchanged.
This decision does not authorize audio acquisition or qualify playback. Existing
incomplete source inventory cannot be silently promoted to admitted metadata.
Deterministic checks and independent review establish this selector's behavior;
actual source access, file integrity and deployment need their own evidence.

This record changes the earlier catalog plan's implementation gate. It does not
supersede or alter the accepted Linux resource experiment in decision 0001.
