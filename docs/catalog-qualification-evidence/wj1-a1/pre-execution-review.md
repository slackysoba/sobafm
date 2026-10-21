# Independent executable review: WJ1, revision a2

Reviewer: `/root/review_175_protocol`. Static exact-byte review only; this reviewer
ran no helper, native measurement, provider request or tests and changed no
production source. `source-index.json` records the complete review boundary and
source base `4fc157559a1036a4e5e3f20ec0591c8000df3541`, tree
`6b77282c0579d323c74ece1a7ba82ea090e86443`. Snapshots preserve reviewed bytes.

| File | SHA256 |
| --- | --- |
| `docs/catalog-qualification.md` | `bd5bfe0d2ee3b68d024916cbe645ffb2479063d622abf7937593e4d645098393` |
| `scripts/qualify_catalog_boundary.py` | `cad70ce80eb7676c2840b73f03b12252591328480b79df28b41e18d797680d38` |
| `scripts/catalog_boundary_fixture.py` | `f09d7e6d7da34a9387575e6b3d3f089ef45f8bd7b3aa0ec01341046af02a6464` |
| `tests/test_catalog_boundary_qualification.py` | `4511fac677649b50854f704e4733183dca9f59b4bd94fc1dfda43dcf89f6e277` |
| `tests/test_track_deck.py` | `dd6a104f1666a93cef40b4e55286a1ae70e6ce2bbe754fbb856eff0436d02359` |

## Disposition

**No blocking findings on the exact a2 executable packet for the single WJ1
mechanism batch described by the reviewed protocol.** This resolves the earlier
protocol and executable findings; it does not relabel their blocked snapshots.
The executor must bind these reviewed source/fixture hashes and the actual
interpreter hashes to the run, retain the one-attempt budget and preserve every
failed, rejected or incomplete result. Changed bytes require another disposition.

## Resolution and verification

- `Case.run` now places start, execution, observation and reporting inside the
  retention `finally`. Observation errors produce a fixed uncertain/failed
  record; reporting errors still enter retention. Registry tasks are shielded
  from repeated caller cancellation, and charged slots prevent process exit even
  if cleanup tasks have ended without verified release. The throwing-observer
  test exercises the previously uncovered path with a pending owner.
- Pass requires ordering `OUTPUT`/failed, memory `DECODER`/failed, or the deliberate
  retired descendant result. False completion, a different failure, unexpected
  output/stderr, cleanup failure or a refunded fixture lease cannot pass.
  Negative controls cover false completion, wrong category and released lease.
- The descendant must produce exactly D/H, have zero observed leader returncode,
  remain in a nonempty Job and retain both non-EOF readers before retirement.
  Subsequent child/pipe/Job release uses the pinned production cleanup and actual
  owned handle checks; direct-child exit alone is not the release oracle.
- The new final-padding regression observes the actual full-buffer padding wait
  and then reschedules a real Timeout. It removes the 30 ms fixture-throughput
  assumption while retaining the production lifetime/cleanup constants and
  checking the intended deadline failure with no completion. No runtime behavior
  is changed. The original a3 timing failure remains historical evidence.

The supplied `runner-offline-a5.log` reports 91 passed in 11.73 seconds and
`runner-types-a5.log` reports zero errors/warnings. Both are hash-bound in the
index. They are retained offline results, not independent reviewer execution or
native-platform evidence.

## Bounds and identity limits

The helper uses isolated installed Python, fixed G input, at most one 144 MiB
allocation and one fixed inherited descendant with no breakaway. There are three
fixed cases, four fixture processes total, two simultaneous, one private Job at a
time, and 64 counted output bytes per pipe. Five-second case response and one
original three-second cleanup decision are separate from the 30-second admission
budget. On uncertain release the same production cleanup/owner remains active;
no new case or deadline reset is permitted. This review accepts that explicitly
documented retained-owner behavior, not an unlimited measurement batch.

The runtime preflight records fixture executable SHA256
`4942b86a6597e5aee0128daa00050ed79bc21f6e709a78eb19cbfeb0c2f39ac9`, valid expected
publisher booleans, version 3.14.7 and AMD64 PE. This reviewer inspected and hashed
that supplied record; it did not independently inspect signer metadata, execute
Python or establish host hardware qualification. The run's actual executable
hash must match it. The runner separately records the parent executable hash.

The rig's native identity and envelope remain synthetic constructor controls;
they never authenticate or execute FFmpeg. Kept fixture charges are mechanism
oracles, not measured audio envelopes. Invalid PCM cannot establish strict
completion or readiness. The initial results still correctly have zero native
measurements/provider calls and all installation rows incomplete. A later actual
WJ1 result may cover only the declared Python/Windows adapter mechanism. It
cannot qualify selected-decoder startup, audio accounting, complete cleanup
matrix, clean installation, native platform coverage, providers, credits,
release/endurance or catalog production trust. Those gates remain incomplete.
