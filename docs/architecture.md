# Architecture

The offline catalog selector is implemented. The station, playback and deployment architecture below remains proposed and unqualified.

SobaFM has one station/player per server. Commands validate access and intent. A mode-specific interpreter produces a validated musical plan. Catalog selection or generated production prepares audio; the connected player settles what listeners actually hear. Settings and remembered channels persist in SQLite.

Catalog preparation happens ahead of playback: source admission, provenance, credits, curation and descriptors. Runtime selection ranks eligible candidates and retrieves audio according to source policy. Start small; add adapters or audio analysis only when useful.

## Implemented catalog boundary

`catalog.py` validates bounded prepared metadata, explicit queries and separate trusted admission reviews. Reviews bind complete credits and access readiness to an immutable recording digest. Unreviewed, changed, unavailable or incomplete recordings remain rejected. No provider or model approval field is accepted in recording metadata.

`catalog_prompt.py` translates a small, explicit phrase vocabulary into that query model. It reports unknown positive terms and refuses unsupported hard constraints rather than guessing. `catalog_demo.py` reads local JSON and a prompt, returning full match evidence, preserved credits, rejection reasons and coverage for each of Jamendo, ccMixter and FMA. It accepts reviewed real metadata as well as explicitly synthetic test fixtures.

`catalog_import.py` converts bounded saved Jamendo tracks responses, ccMixter info responses and explicit FMA operator manifests into the same validated snapshot schema. Source-specific field maps preserve identity, credit evidence and unknowns. A separate report records malformed rows, missing requested IDs and bounded source coverage; conflicting duplicate identities are quarantined together. Outputs contain no media URLs or generated admission decisions. The importer's draft metadata still needs complete-kind/access evidence and separate credit review before selection admission. See [saved metadata imports](catalog-evaluation.md#saved-source-metadata-imports).

Selection compares every supplied partition before applying the result limit. Strong supported preferences precede weaker evidence and duration; hard constraints filter first. Partition completeness describes only the dated prepared subset. The modules perform no provider calls, audio access, state changes or Discord operations. See the [selection contract and demo](catalog-evaluation.md#current-selection-contract) and [accepted decision](decisions/0002-prompt-catalog-selection.md).

The separate `ccmixter_proof.py` evaluation uses two closed typed recording definitions in `ccmixter_cases.py`: WILD WINDSTEPS (70993/file129564) and the complete Fruitless mix (70072/file125900). Each case can recheck its six fixed metadata responses and acquire one temporary candidate file only after explicit invocation with a reviewed, candidate-bound fingerprint manifest. Its versioned policy normalizes only a unique, bounded decimal value inside an exact hidden HTML anchor context; all other page bytes and the complete info JSON remain protected. Only the selected case's nominated info URLs additionally accept `text/plain`; byte-exact hashing, duplicate-key-rejecting JSON parsing and candidate validation remain mandatory. Receipts retain a bounded MIME type/subtype without parameters or other response headers. Raw response hashes stay separate from comparison fingerprints. Case, catalog digest, projection/context and complete manifest binding precede resource creation. Child requests carry the case identity; cross-case routes fail before HTTP. File selection is by unique ID; stems and Fruitless upstream Vox/Guitar files cannot become playback candidates. Its single request owner retains processes, counters and files across cancellation and failed cleanup; there are no automatic retries. It reuses the importer and catalog digest without granting admission. Decoding, embedded notices and complete-file validation remain pending. See the [finite source proof and execution boundary](../evaluations/catalog/source-proof.md); this is not a production provider or playback implementation.

The audio callback consumes prepared PCM without blocking. Producers and cleanup retain explicit ownership across cancellation. Capacity admission includes retained work and cleanup debt. Prove the chosen Linux enforcement configuration before depending on it throughout the application.

The separate Linux-only `fma_intake.py` evaluation owns one explicitly supplied local Night Owl copy in one private destination slot. Its closed v2 request binds the unadmitted catalog, notices, exact file identity/hash, a neutral retention anchor with exact/lower-bound basis and bounded operator evidence, and fixed expiry. Historical v1 records retain exact-time semantics and fingerprints. One initially inert validation consumer may borrow the registered read-only descriptor for synchronous seekable inspection, then receive a bounded, identity/hash/count-verified stream. Reader and downstream handles, operations and backing remain owned across cancellation until actual join/close; blocking callbacks never hold the admission lock. Recovery is cleanup-only and refuses uncertain persisted reader ownership. A fresh local helper may inherit the descriptor through maintained process facilities; this does not establish Docker descriptor transport or authorize native execution. The supplied original remains untouched during this synthetic implementation work, and no catalog admission or availability changes. See the [operator workflow and reader contract](../evaluations/catalog/fma-local-intake.md).

## Decisions to establish first

The opt-in [Night Owl validation evaluation](../evaluations/fma_validation/README.md)
connects that one-copy intake to sequential contained Mutagen inspection and a
fresh mpg123 streaming runner under maintained init. A closed source/image/input/
expiry packet and finite external Docker controller retain process, I/O, payload
and volume debt through actual release. Synthetic checks exercise this source;
fresh topology fit, native terminal-filter qualification and actual recording
validation remain separate pending execution gates. This adds no playback service
or catalog admission and does not alter the accepted resource ADR.

- Source-specific acquisition/storage implementations for the accepted three-source pool, with complete recording credits and access evidence.
- The simplest viable non-root Docker resource model under the approved capacity definition.
- Supported dependencies and decoder distribution without a bespoke supply system by default.

Record those decisions with evidence. Update this document to describe actual modules and data flow as they are implemented; do not pre-create a framework from this outline.

See [requirements](requirements.md) for product behavior and [engineering notes](engineering-notes.md) for implementation constraints and regression scenarios.
