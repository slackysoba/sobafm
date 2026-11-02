# Evaluation dependency notices

This opt-in evaluation adds **Mutagen 1.48.1**, copyright its upstream authors,
licensed **GPL-2.0-or-later**. Preserve upstream copyright notices, the included
[COPYING](COPYING), and corresponding source when distributing a covered combined
evaluation. Process separation does not remove these obligations. Existing SobaFM
MIT notices remain intact. This change does not adopt Mutagen as an application
dependency or relicense unrelated repository material.

The [PyPI release](https://pypi.org/project/mutagen/1.48.1/),
[release metadata](https://pypi.org/pypi/mutagen/1.48.1/json), and
[upstream license](https://raw.githubusercontent.com/quodlibet/mutagen/release-1.48.1/COPYING)
were reviewed on 2026-11-02. The June 2026 release follows a long release interval
and includes current fixes. It supports Python >=3.10,<4, has no runtime
dependencies and distributes a platform-independent wheel. The inspected wheel
contains 70 files and 632,227 expanded bytes, without native binaries or `.pth`
startup hooks. This fits the existing Python/Linux toolchain without a native build.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `mutagen-1.48.1-py3-none-any.whl` | 195,706 | `4f077fe87d3fc7fba259aa63d8c026b18382ca6a42ef37c61e16f1b1b5b82fe7` |
| `mutagen-1.48.1.tar.gz` | 1,276,978 | `8f95637ab9f6f305cec6bd1294e197debe207998e3e068596563c74f86b0a173` |

Both exact artifacts were size/hash verified; `uv.lock` binds this version in the
evaluation dependency group. Retain the matching source archive alongside any
distributed evaluation image, with the wheel's original notices. PyPI reported
no listed vulnerabilities at review; this is limited release metadata, not a
security audit. No Trusted Publishing or artifact attestation was supplied.

Mutagen can allocate whole tags and decompress frame payloads. The helper applies
address/data/CPU limits and an alarm before importing/parsing; a separate framing
pass rejects unsupported transformations before library allocation. It bounds
tag bytes, reads, seeks, frames and selected notices. The supported parser subset
is fixed-framing v2.3 plus separate v1: nested chapters, v2.4 repair, duplicate
frame IDs and COMR are explicit failures. The public `known_frames` parameter
keeps unselected frames opaque; decoded source payload is bounded separately
before Unicode/key allocation. Canonical JSON size is checked before encoding.
Large/unsupported evidence
fails rather than being silently truncated. Fresh whole-group fit qualification
is still required. Version-specific use of `ParseID3v1` is isolated in this
evaluation and covered by synthetic v1/v2 conflict tests.

The native mpg123 1.33.7 executable and reviewed ELF dependency identities remain
those of the existing native proof. Their upstream licensing and supply evidence
remain applicable to those unchanged files; the new Python/metadata image and
process topology require their own qualification.
