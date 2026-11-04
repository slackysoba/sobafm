# 0005: Use disnake for Discord voice

Status: **Selected** (2026-11-04) under the dependency choice delegated by issue
[#70](https://github.com/slackysoba/sobafm/issues/70). It becomes accepted when
it merges after the issue's required checks and independent review.
Parent: [#2](https://github.com/slackysoba/sobafm/issues/2).
Slice decision: [ADR 0004](0004-amd64-whole-service-adoption.md).

## Problem

The first playback slice needs a maintained Discord library that can join a
voice channel and send audio. No library had been selected. This record covers
selection and locking only. It adds no wrapper, adapter, token, gateway or voice
activity.

Discord's current voice requirements were reviewed on 2026-11-04 in the official
developer documentation, using the
[discord/discord-api-docs source at `da52492`](https://github.com/discord/discord-api-docs/blob/da52492ced82d375981c46618cc3e182daada9a2/developers/topics/voice-connections.mdx)
(committed 2026-11-02; discord.com could not be reached from the review
environment):

- **Transport encryption.** `aead_xchacha20_poly1305_rtpsize` is marked
  "Available (Required)" and `aead_aes256_gcm_rtpsize` "Available (Preferred)".
  All other modes are deprecated and were "discontinued as of November 18th,
  2024".
- **End-to-end encryption (DAVE).** "we will **only support E2EE calls starting
  on March 1st, 2026** for all audio and video conversations in … voice
  channels". The
  [2025-09-02 changelog entry](https://github.com/discord/discord-api-docs/blob/da52492ced82d375981c46618cc3e182daada9a2/developers/change-log.mdx)
  adds: "any client or application not updated for DAVE support will no longer
  be able to participate in Discord calls". A library without DAVE therefore
  cannot play audio.
- **Voice gateway.** Version 8 is recommended. Versions below 4 were rejected
  from 2024-11-18.

## Options

Facts were reviewed on 2026-11-04. Release dates and dependency ranges come from
each project's PyPI JSON metadata (`https://pypi.org/pypi/<name>/json`).
Advisory data comes from PyPI's per-version `vulnerabilities` field.

| Option | Latest release | License | Python 3.14 | DAVE | Finding |
| --- | --- | --- | --- | --- | --- |
| **[disnake](https://github.com/DisnakeDev/disnake) 2.12.2 `[voice]`** | 2026-10-08; commits to [2026-10-23](https://github.com/DisnakeDev/disnake/commit/23f80327fab80509853f37d73ce3210981c56fd4) | MIT | Classifier, and its CI matrix uses the classifiers | [dave.py](https://github.com/DisnakeDev/dave.py) 1.1.0 (2026-10-25), binding Discord's libdave v1.2.2 | **Selected.** Sends only `aead_xchacha20_poly1305_rtpsize`, uses voice gateway v8 and advertises `max_dave_protocol_version`. PyNaCl range `>=1.5.0,<1.7` allows the patched 1.6.2. |
| [py-cord](https://github.com/Pycord-Development/pycord) 2.8.1 `[voice]` | 2026-07-25; commits to [2026-11-01](https://github.com/Pycord-Development/pycord/commit/eb925ceedb812d558b1ace4e6fa697c417842c74) | MIT | Classifier and CI | [davey](https://github.com/Snazzah/davey) (independent Rust implementation), range `>=0.1.4,<1` | Rejected for now. Its range admits davey 0.2.0 (2026-11-02), whose [changelog](https://github.com/Snazzah/davey/blob/1c37646d4c4fa7e69e2fa00cfbe3277d3a7ff3fc/davey-python/CHANGELOG.md) is breaking: keys switch only after `prepare_transition()`/`execute_transition()`. py-cord's gateway code at `eb925ce` calls neither. davey 0.1.7, the last version py-cord was written for, predates the 0.2.0 fix for nonce reuse on key re-install. This is a code-reading finding, not a runtime test. |
| [discord.py](https://github.com/Rapptz/discord.py) 2.7.1 `[voice]` | 2026-03-03; last commit [2026-09-01](https://github.com/Rapptz/discord.py/commit/65232c38702be5844cf2ce865a4777eb1928b5d0) | MIT | No 3.13/3.14 classifier | davey `>=0.1.0` (unbounded, so it admits the breaking 0.2.0) | Rejected. The released `voice` extra requires `PyNaCl<1.6`, which forces PyNaCl 1.5.0 and its advisory [GHSA-mrfv-m5wm-5w6w](https://github.com/advisories/GHSA-mrfv-m5wm-5w6w) (CVE-2025-69277, fixed in 1.6.2). The fix is only on the unreleased main branch. |
| [nextcord](https://github.com/nextcord/nextcord) 3.2.0 `[voice]` | 2026-05-21 | MIT | Classifier | dave-py `<0.2.0` | Rejected. Its range `PyNaCl<1.6` forces the same advisory, and its dave-py range excludes the maintained 1.x line. |
| [hikari](https://github.com/hikari-py/hikari) 2.7.0 | 2026-10-25 | MIT | Classifier | None | Rejected. It has no voice implementation and would need a second voice stack. |

A custom fork, a native rebuild or overriding a library's declared dependency
range would work around these findings. The issue excludes all three.

## Decision

Add `disnake[voice]==2.12.2` as the first application runtime dependency. The
voice extra brings PyNaCl (transport encryption), dave.py (DAVE) and audioop-lts.
uv locks the dependency closure exactly. No application code uses it yet.
Ordinary module boundaries are the only replaceability measure, with no
provider abstraction layer.

## Native components

These locked distributions ship compiled code as wheels. Each has a CPython 3.14
manylinux wheel for both x86_64 and aarch64, so neither architecture builds
from source. The table describes Linux wheel contents inspected on amd64.

| Distribution | Native content | License(s) |
| --- | --- | --- |
| dave-py 1.1.0 | `_dave_impl` extension statically linking libdave v1.2.2 (via the [DisnakeDev/libdave fork at `1e21de0`](https://github.com/DisnakeDev/libdave/tree/1e21de0e99466f729380c48451b44ac759f8eae6)), mlspp, nanobind, nlohmann-json and OpenSSL 3 libcrypto. It dynamically links libstdc++/libgcc_s/glibc. | MIT (dave.py, libdave, nlohmann-json); BSD-2-Clause (mlspp); BSD-3-Clause (nanobind); Apache-2.0 (OpenSSL). All license texts are shipped in the wheel. |
| PyNaCl 1.6.2 | `_sodium` extension with bundled libsodium | Apache-2.0; libsodium ISC |
| cffi 2.1.1 | `_cffi_backend` extension (PyNaCl dependency) | MIT-0 |
| audioop-lts 0.2.2 | `_audioop` extension | PSF-2.0 |
| aiohttp 3.14.5 | HTTP parser (llhttp), writer and websocket extensions | Apache-2.0 AND MIT |
| multidict 7.2.1, yarl 1.25.1, propcache 0.5.4, frozenlist 1.8.0 | Optional C speedup extensions | Apache-2.0 |

**System libopus is not in the lock.** disnake encodes Opus through
`ctypes.util.find_library("opus")`. The application image must provide libopus
(BSD-3-Clause) and record its version. That belongs to the image work, not this
change.

## Consequences and risks

- **Bundled OpenSSL 3.0.7.** libdave's
  [OpenSSL 3 vcpkg manifest](https://github.com/DisnakeDev/libdave/blob/1e21de0e99466f729380c48451b44ac759f8eae6/cpp/vcpkg-alts/openssl_3/vcpkg.json)
  overrides OpenSSL to 3.0.7, a 2022 release. Upstream
  [discord/libdave](https://github.com/discord/libdave/blob/main/cpp/vcpkg-alts/openssl_3/vcpkg.json)
  carries the same override (checked 2026-11-04). The dave-py wheel therefore
  links a libcrypto that no OpenSSL security update reaches without a new
  dave-py release. It is used for MLS and AES-GCM frame cryptography on
  messages relayed by the voice gateway, not for TLS. Affected advisories have
  not been assessed individually. This is accepted as a visible residual risk
  under the existing native supply-chain requirement
  ([requirements](../requirements.md#deployment-privacy-and-supply-chain)), not
  as a security exception. Revisit it on any dave.py or libdave release and
  before the Outcome 4 dependency check. A fork or local rebuild would need a
  new decision.
- **Silent degradation.** disnake disables transport encryption or DAVE when
  PyNaCl or dave.py fails to import. `tests/test_voice_dependency.py` checks,
  in the installed environment on both CI architectures, that both native
  modules load and that the required transport mode and DAVE version 1 are
  available. This makes no network or voice contact and does not establish
  voice behavior.
- **Gateway intents.** disnake allows the requirement to request only the
  Guilds and Voice States intents. The configuration happens when the
  connection is implemented.
- **Native arm64.** Maintained aarch64 wheels exist for every native component
  above. This is a note only, for [#60](https://github.com/slackysoba/sobafm/issues/60).
- The controlled-capacity definition is unchanged. Native voice
  encoding/encryption/packet backing is already an explicit exclusion in the
  requirements. This dependency adds no payload pool, and the decision makes no
  memory claim.

## Verification needed

- `uv sync --locked`, all pre-commit checks and pytest on both CI
  architectures. The arm64 result is the CI job's result.
- Independent review of this selection and the lock diff.
- At the first voice-connection slice, authorized live evidence that disnake
  negotiates `aead_xchacha20_poly1305_rtpsize` and DAVE. Offline checks cannot
  show that.
- At image build, the libopus version and license, and an SBOM covering the
  statically linked components above.
