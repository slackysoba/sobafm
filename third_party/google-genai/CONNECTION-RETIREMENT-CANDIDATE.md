# Connection retirement package verification

Prepared for #240 on 2026-10-23 from application base
`4e429180923cc0a1afd1648f28e551a3c15810f5` with signed CPython 3.14.7
and the existing hash-pinned build requirements. The exact artifacts were
[published on 23 October 2026](https://github.com/slackysoba/sobafm/releases/tag/sdk-google-genai-2.26.0-sobafm.3)
from reviewed commit `3fd0f06ce99a8c444ad4b90e2231962dd2655978`, following
[explicit maintainer approval](https://github.com/slackysoba/sobafm/issues/240#issuecomment-6365097319).
The application now pins that wheel and SHA-256. Published preparation material
bytes, including the historical candidate status, remain unchanged.

## Immutable candidate

Version: `2.26.0+sobafm.3`.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `google_genai-2.26.0+sobafm.3-py3-none-any.whl` | 1177347 | `ce3a2bef488dbd3c13906c287fea8fa5920a2b1a8709af090fadccb752916126` |
| `google_genai-2.26.0+sobafm.3.tar.gz` | 766049 | `bbb1e50a4e9c4389206605cbe250a60786a7190d8c3f07c4037564865cdd7cbb` |

Retained output: `C:/dev/sobafm/.local/g240-a6`, including `candidate.json`,
`prepared.json`, `backend.log`, complete source and both distributions.
The new notice names `.3`; earlier candidate artifacts and evidence are retained.
The recipe verified exact upstream/base/extension source, packaged SDK bytes,
notices, wheel/source archive correspondence and bundled build materials.
`CONNECTION-RETIREMENT.toml` pins the changed source and patch.

## Installed verification

The corrected `g240-a6` wheel was installed and tested in the owned SobaFM
environment. All 20 actual SDK and Deck fake-I/O checks passed:
10 existing compatibility checks and 10 candidate identity/retirement checks.
The new checks include successive reuse without forced GC, queued audio,
partial-stream/parser failure, receive/setup failure and cancellation, repeated
waiter cancellation, late closure with a sticky failed verdict, retained PCM and
unsupported teardown retaining backing/debt. Strict checking of the changed SDK
adapter and receiver reported zero errors. Test logs are retained under
`C:/dev/sobafm/.local/issue240-author-20261023-a1`.

Preparation verification restored published `.2` after isolated candidate tests.
Activation verification uses a fresh locked sync to install published `.3`;
normal distribution tests assert its version, public URL, metadata and source
hashes. The actual installed SDK/Deck checks also run against that public wheel. The additional
repository regression checks the new patch pin and preserved published build
materials. The final PR records ordinary hook, Windows/Linux type and complete
offline results. The normal Security scan binds the derived `.3` identity to
official `google-genai==2.26.0` using the existing explicit upstream input and
verified Apache-2.0/MIT metadata. No vulnerability waiver or license policy change
is made. This record does not grant readiness or a memory profile.
