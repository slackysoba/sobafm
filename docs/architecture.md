# Architecture

The offline catalog selector is implemented. The station, playback and deployment architecture below remains proposed and unqualified.

SobaFM has one station/player per server. Commands validate access and intent. A mode-specific interpreter produces a validated musical plan. Catalog selection or generated production prepares audio; the connected player settles what listeners actually hear. Settings and remembered channels persist in SQLite.

Catalog preparation happens ahead of playback: source admission, provenance, credits, curation and descriptors. Runtime selection ranks eligible candidates and retrieves audio according to source policy. Start small; add adapters or audio analysis only when useful.

## Implemented catalog boundary

`catalog.py` validates bounded prepared metadata, explicit queries and separate trusted admission reviews. Reviews bind complete credits and access readiness to an immutable recording digest. Unreviewed, changed, unavailable or incomplete recordings remain rejected. No provider or model approval field is accepted in recording metadata.

`catalog_prompt.py` translates a small, explicit phrase vocabulary into that query model. It reports unknown positive terms and refuses unsupported hard constraints rather than guessing. `catalog_demo.py` reads local JSON and a prompt, returning full match evidence, preserved credits, rejection reasons and coverage for each of Jamendo, ccMixter and FMA. It accepts reviewed real metadata as well as explicitly synthetic test fixtures.

`catalog_import.py` converts bounded saved Jamendo tracks responses, ccMixter info responses and explicit FMA operator manifests into the same validated snapshot schema. Source-specific field maps preserve identity, credit evidence and unknowns. A separate report records malformed rows, missing requested IDs and bounded source coverage; conflicting duplicate identities are quarantined together. Outputs contain no media URLs or generated admission decisions. The importer's draft metadata still needs complete-kind/access evidence and separate credit review before selection admission. See [saved metadata imports](catalog-evaluation.md#saved-source-metadata-imports).

Selection compares every supplied partition before applying the result limit. Strong supported preferences precede weaker evidence and duration; hard constraints filter first. Partition completeness describes only the dated prepared subset. The modules perform no provider calls, audio access, state changes or Discord operations. See the [selection contract and demo](catalog-evaluation.md#current-selection-contract) and [accepted decision](decisions/0002-prompt-catalog-selection.md).

The separate `ccmixter_proof.py` evaluation can recheck six fixed metadata responses and acquire one temporary candidate file only after explicit invocation with a reviewed, candidate-bound fingerprint manifest. Its single request owner retains processes, counters and files across cancellation and failed cleanup; there are no automatic retries. It reuses the importer and catalog digest without granting admission. Decoding, embedded notices and complete-file validation remain pending. See the [finite source proof and execution boundary](../evaluations/catalog/source-proof.md); this is not a production provider or playback implementation.

The audio callback consumes prepared PCM without blocking. Producers and cleanup retain explicit ownership across cancellation. Capacity admission includes retained work and cleanup debt. Prove the chosen Linux enforcement configuration before depending on it throughout the application.

## Decisions to establish first

- Source-specific acquisition/storage implementations for the accepted three-source pool, with complete recording credits and access evidence.
- The simplest viable non-root Docker resource model under the approved capacity definition.
- Supported dependencies and decoder distribution without a bespoke supply system by default.

Record those decisions with evidence. Update this document to describe actual modules and data flow as they are implemented; do not pre-create a framework from this outline.

See [requirements](requirements.md) for product behavior and [engineering notes](engineering-notes.md) for implementation constraints and regression scenarios.
