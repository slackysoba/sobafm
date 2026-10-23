# Connection retirement package verification

Prepared for #240 on 2026-10-23 from application base
`4e429180923cc0a1afd1648f28e551a3c15810f5` with signed CPython 3.14.7
and the existing hash-pinned build requirements. No dependency activation,
release tag or publication is included.

## Immutable candidate

Version: `2.26.0+sobafm.3`.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `google_genai-2.26.0+sobafm.3-py3-none-any.whl` | 1177346 | `c6b2d5ab8eef97c7cafc9e3da66ba84551af763c569107c398d26cdb368e2876` |
| `google_genai-2.26.0+sobafm.3.tar.gz` | 766252 | `9cef1a90a400ce54566ea0cf0f9a29a67ff426be3c3fac35401f485d81e69fe5` |

Retained output: `C:/dev/sobafm/.local/g240-a5`, including `candidate.json`,
`prepared.json`, `backend.log`, complete source and both distributions.
The recipe verified exact upstream/base/extension source, packaged SDK bytes,
notices, wheel/source archive correspondence and bundled build materials.
`CONNECTION-RETIREMENT.toml` pins the changed source and patch.

## Installed verification

The final wheel is byte-identical to the installed `g240-a4` wheel tested in the
owned SobaFM environment. All 20 actual SDK and Deck fake-I/O checks passed:
10 existing compatibility checks and 10 candidate identity/retirement checks.
The new checks include successive reuse without forced GC, queued audio,
partial-stream/parser failure, receive/setup failure and cancellation, repeated
waiter cancellation, late closure with a sticky failed verdict, retained PCM and
unsupported teardown retaining backing/debt. Strict checking of the changed SDK
adapter and receiver reported zero errors. Test logs are retained under
`C:/dev/sobafm/.local/issue240-author-20261023-a1`.

Application verification uses a fresh locked sync to restore published `.2`;
normal distribution tests continue to assert that active identity. The additional
repository regression checks the new patch pin and preserved published build
materials. The final PR records ordinary hook, Windows/Linux type and complete
offline results. Publication/adoption must use these exact reviewed artifacts and
receive separate clearance; this record does not grant readiness or a memory
profile.
