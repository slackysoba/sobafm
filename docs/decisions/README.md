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
| [0007](0007-bound-catalog-audio-decoding.md) | Bound catalog audio decoding before choosing a runtime tool | Proposed |
