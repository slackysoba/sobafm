# Architecture decision records

Each record captures one material decision: its context, the options considered, the choice, and its consequences.

## When to write an ADR

- Choosing a language, framework, service, or dependency that would be costly to reverse
- A design with meaningful tradeoffs that later work must respect
- Accepting a risk or a temporary exception

## Process

1. A `decision` issue states the question, the requirements, the options, and a recommendation.
2. A pull request adds `NNNN-title-in-kebab-case.md`, copied from the [template](template.md), with the status the record will have once merged, normally Accepted. The pull request is the proposal.
3. The maintainer approves the decision by merging the pull request.
4. Accepted records are not rewritten. A new record supersedes an old one, and the old record's status changes to `Superseded by ADR-NNNN`.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-build-on-python-discord-py-and-the-google-gen-ai-sdk.md) | Build on Python 3.14, discord.py 2.7, and the Google Gen AI SDK | Accepted |
| [0002](0002-interpret-requests-with-gemini-structured-output.md) | Interpret requests with Gemini structured output | Accepted |
| [0003](0003-keep-server-state-in-sqlite-and-use-discord-command-permissions.md) | Keep server state in SQLite and use Discord command permissions | Accepted |
| [0004](0004-stream-lyria-realtime-through-buffered-decks.md) | Stream Lyria RealTime through buffered decks and a crossfading mixer | Accepted |
| [0005](0005-release-images-with-github-actions-and-artifact-attestations.md) | Release images with GitHub Actions and artifact attestations | Accepted |
| [0006](0006-play-catalog-and-generated-music-in-one-station.md) | Play catalog and generated music in one station | Accepted on merge of PR #156; proposed until then |
| [0007](0007-bound-catalog-audio-decoding.md) | Decode catalog audio through a supervised FFmpeg child | Accepted |
| [0008](0008-fail-lyria-producers-at-qualified-ingress-bounds.md) | Fail Lyria producers at qualified ingress bounds | Accepted on merge of PR #206; Proposed until then |
| [0009](0009-raise-the-per-server-audio-footprint-ceiling-to-70-mb.md) | Raise the per-server audio footprint ceiling to 70 MB | Proposed until its decision pull request is accepted and merged |
| [0010](0010-maintain-a-pinned-bounded-genai-distribution.md) | Maintain a pinned bounded Google Gen AI distribution | Proposed; accepted on decision PR merge after final maintainer acceptance |

ADR-0009 proposes superseding only the 40,000,000-byte total and fixed
38,000,000-byte parent clauses in ADR-0004/0006/0007/0008. Their accepted source
remains intact, and their other provisions remain in force. The proposed
qualified parent/native split shares the strict total without an independent
fixed parent partition.
