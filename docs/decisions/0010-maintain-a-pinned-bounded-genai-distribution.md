# ADR-0010: Maintain a pinned bounded Google Gen AI distribution

- **Status:** Proposed; accepted on decision PR merge after final maintainer acceptance
- **Date:** 2026-10-22
- **Issue:** [#228](https://github.com/slackysoba/sobafm/issues/228), parent [#175](https://github.com/slackysoba/sobafm/issues/175), M5 [#114](https://github.com/slackysoba/sobafm/issues/114)

## Context

[ADR-0001](0001-build-on-python-discord-py-and-the-google-gen-ai-sdk.md) selects
the Google Gen AI SDK. Accepted
[ADR-0008](0008-fail-lyria-producers-at-qualified-ingress-bounds.md) permits an
explicit producer failure before exhausting a complete pre-reserved ingress
allowance, preserving admitted PCM and actual cleanup ownership. Neither record
supplies a bounded SDK integration or a qualified production envelope.

The already reviewed optional patch against locked google-genai 2.26.0 and
WebSockets 16.1.1 exposes public `live_music.BoundedReceiveOptions` and
`live_music.ShapeLimits`. It uses WebSockets' public streaming/options interface
for both setup and later receives. Fixed borrowed storage, fragment limits and
lexical shape checks precede whole-message copies, JSON parsing and model
conversion. Failed or partially consumed owners remain poisoned and retain the
connection/storage until existing context-owned closure. Omitted options preserve
the reviewed legacy/default path, including compression defaults.

This is a downstream SDK extension, not an available stock Google API. The
[concrete materials](../../third_party/google-genai/README.md) include the exact
patch, complete upstream source identity, licenses, Windows recipe and actual
isolated candidate checks. They make the maintenance choice reviewable without
assuming an upstream release or selecting an upstream waiting strategy.

## Options considered

| Option | Behavior and benefit | Responsibility and limitation |
| --- | --- | --- |
| **Maintain the small pinned derived distribution (recommended)** | Reuse the reviewed patch, upstream SDK/backend and public WebSockets APIs; provide an opt-in bounded ingress seam while defaults remain compatible. | SobaFM owns the patch, artifact/source availability, updates and verified advisory identity. The derived package is not stock/Google-supported and still supplies no complete resource proof. |
| **Keep the current stock dependency** | Preserve the current distribution and upstream update path. | The reviewed bounded-receive integration remains unadopted. This does not prove every possible future supported route impossible or require waiting for upstream. |

## Proposed decision

Accept responsibility for a separately identified `google-genai 2.26.0+sobafm.1`
distribution containing only the reviewed three-path semantic patch and explicit
version/license/source-packaging changes. Preserve the original patch and record
its mechanically canonicalized distribution encoding, requiring identical
resulting SDK bytes. Mark modified files. Preserve upstream Apache-2.0/copyright;
distribute SobaFM's
added helper code under MIT with its separate license and attribution.

Use the upstream setuptools backend with recorded pinned build inputs. Verify the
exact upstream archive before safe extraction into a new owned build copy. Build
a universal wheel and matching complete patched sdist; check source/package
correspondence and retain actual artifact hashes and partial/failure outputs.
The corresponding source includes the patch, provenance, recipe, requirements
and notices before any later artifact distribution.

If implementation is subsequently authorized, the maintainer publishes those
artifacts under a dedicated non-`v*` SDK tag in the existing repository. An
ordinary direct wheel requirement with an actual immutable URL/hash replaces the
Google SDK dependency, and the lockfile records that artifact. This works for uv
and ordinary SobaFM package consumers; a uv-only local source override does not
carry the choice into ordinary package dependency metadata. No runtime install
hook, private SDK mutation, global monkeypatch, custom transport or new index is
part of the integration.

Acceptance of this ADR resolves the maintenance/distribution mechanism choice.
This proposal PR only prepares and verifies isolated candidates. It does not
replace application dependencies/lock, publish artifacts/create tags, activate
bounded receive in production, select a numerical profile or grant readiness.
Those concrete implementation actions need their separately authorized scope;
material deviations from this decision return to the maintainer.

## Verification, supply and security

The [package recipe and checks](../../third_party/google-genai/README.md) verify
the fixed upstream/patch inputs, changed paths, derived metadata, actual installed
imports, licenses and complete source. Small mocks exercise actual candidate
SDK sessions and response conversion with owned fake I/O, including omitted
options, both receive phases, shape/copy ordering, UTF-8 diagnostics, cancellation,
busy borrowed storage, poison/nonreuse and context-owned closure. No real Client,
default auth, provider, socket/TLS or native experiment is required.

The candidate manifest records both upstream PyPI advisory identity and derived
artifact pedigree. Eventual adoption must preserve normal lock/transitive scanning
and dependency review and demonstrate upstream-component coverage for the direct
URL/local version. Unknown or unmatched coverage cannot be represented as a
clean scan. Upstream advisory matching does not review the local patch. This
record creates no scanner exception, ignore, new advisory sweep or security waiver.

SobaFM maintains the patch and artifact/source availability. An upstream update
requires a new verified base, review of patch reapplication, package tests,
dependency/security coverage and new immutable version/digests. A fixed URL does
not inherit registry updates. Prefer an adequate public upstream interface if one
is actually available later; no such future release is assumed here.

## Consequences and rollback

The proposed carrier makes the existing bounded seam installable and auditable,
at the cost of a small downstream distribution and explicit update obligations.
It does not establish JSON/model/base64, WebSocket/TLS/socket, parent/native/player
or cleanup allocation bounds. Whole-system qualification and useful capacity
remain #175 obligations.

Keeping or reverting the application to the current stock dependency leaves the
optional bounded path inactive and its ingress integration unresolved. After any
future adoption, rollback must be a reviewed dependency/lock change; it cannot
preserve a claim that the optional ingress guard remains active.

Accepted decision sources remain unchanged.
[ADR-0009](0009-raise-the-per-server-audio-footprint-ceiling-to-70-mb.md), accepted
in PR #219, now sets the strict 70,000,000-byte total with a qualified parent/native
split. That ceiling revision and the PCM ownership implementation merged in
PR #227 remain independent of this proposal. This decision selects no production
envelope. PCM stays 34,560,000 bytes, and the original single three-second cleanup
deadline/debt rules remain in force. No source, rights, readiness,
latency/endurance or shipping gate is waived.
