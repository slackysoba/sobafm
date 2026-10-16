"""Bind collected Debian sources/binaries to the pinned builder's signed indices."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cleartext(text: str) -> str:
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----\n"):
        return text
    body = text.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----", 1)[0]
    return "\n".join(line.removeprefix("- ") for line in body.splitlines()) + "\n"


def paragraphs(text: str) -> Iterator[dict[str, str]]:
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")):
                if not key:
                    raise ValueError("metadata continuation lacks a field")
                fields[key] += "\n" + line[1:]
            elif line:
                key, separator, value = line.partition(":")
                if not separator or key in fields:
                    raise ValueError("invalid or duplicate metadata field")
                fields[key] = value.lstrip()
        if fields:
            yield fields


def checksums(value: str) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for line in value.splitlines():
        if not line.strip():
            continue
        sha, size, name = line.split()
        if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
            raise ValueError("invalid SHA-256 metadata")
        if name in result or int(size) < 0:
            raise ValueError("ambiguous checksum metadata")
        result[name] = sha, int(size)
    if not result:
        raise ValueError("checksum metadata is absent")
    return result


def verify_file(path: Path, expected: tuple[str, int]) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"authenticated material is absent or linked: {path.name}")
    if path.stat().st_size != expected[1] or digest(path) != expected[0]:
        raise ValueError(f"authenticated material digest mismatch: {path.name}")


def source_files(root: Path, record: dict[str, str]) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    for name, expected in sorted(checksums(record["Checksums-Sha256"]).items()):
        if Path(name).name != name or name in {".", ".."} or "\\" in name:
            raise ValueError("source metadata has a nonlocal filename")
        path = root / name
        verify_file(path, expected)
        records.append({"name": name, "sha256": expected[0], "size": expected[1]})
    return records


def authenticate(toolchain: Path, evidence: Path) -> None:
    apt = toolchain / "apt"
    home = evidence / "public-debian-gnupg"
    home.mkdir(mode=0o700)
    keyring = apt / "debian-archive-keyring.gpg"
    releases: dict[str, dict[str, tuple[str, int]]] = {}
    authenticated_releases: list[dict[str, str]] = []
    for release in sorted(apt.glob("*InRelease")):
        verification = subprocess.run(  # noqa: S603 - fixed tool, public pinned metadata
            [
                "/usr/bin/gpg",
                "--no-options",
                "--homedir",
                str(home),
                "--no-default-keyring",
                "--keyring",
                str(keyring),
                "--batch",
                "--no-auto-key-retrieve",
                "--status-fd",
                "1",
                "--verify",
                str(release),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        signatures = [
            line.split()[2]
            for line in verification.stdout.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        if not signatures:
            raise ValueError("Debian metadata has no verified archive signature")
        metadata = next(paragraphs(cleartext(release.read_text(encoding="utf-8"))))
        releases[release.name.removesuffix("_InRelease")] = checksums(metadata["SHA256"])
        authenticated_releases.append(
            {
                "name": release.name,
                "sha256": digest(release),
                "signers": ",".join(sorted(signatures)),
            }
        )
    if not releases:
        raise ValueError("signed Debian release metadata is missing")
    covered = [
        line.split("\t") for line in (toolchain / "covered-packages.tsv").read_text().splitlines()
    ]
    if any(len(row) != 4 for row in covered):
        raise ValueError("invalid covered package inventory")
    binary_keys = {(row[0].split(":")[0], row[1]) for row in covered}
    source_keys = {(row[2], row[3]) for row in covered}
    binaries: dict[tuple[str, str], dict[str, str]] = {}
    sources: dict[tuple[str, str], dict[str, str]] = {}
    indices: list[dict[str, str | int]] = []
    for index in sorted(apt.glob("*.lz4")):
        prefix, separator, suffix = index.name.partition("_main_")
        if not separator or prefix not in releases:
            raise ValueError("index lacks matching signed release metadata")
        relative = "main/" + suffix.removesuffix(".lz4").replace("_", "/")
        expected = releases[prefix].get(relative)
        if expected is None:
            raise ValueError("index is absent from signed release checksums")
        result = subprocess.run(  # noqa: S603 - fixed decompressor, collected public index
            ["/usr/lib/apt/apt-helper", "cat-file", str(index)],
            capture_output=True,
            check=True,
        ).stdout
        if len(result) != expected[1] or hashlib.sha256(result).hexdigest() != expected[0]:
            raise ValueError("index differs from the signed release checksum")
        indices.append({"name": index.name, "sha256": expected[0], "size": expected[1]})
        is_source = relative == "main/source/Sources"
        keys, selected = (source_keys, sources) if is_source else (binary_keys, binaries)
        for record in paragraphs(result.decode("utf-8")):
            key = record.get("Package", ""), record.get("Version", "")
            if key in keys:
                if key in selected:
                    previous = selected[key]
                    consistent = (
                        checksums(previous["Checksums-Sha256"])
                        == checksums(record["Checksums-Sha256"])
                        if is_source
                        else all(
                            previous.get(field) == record.get(field)
                            for field in ("SHA256", "Size", "Source", "Architecture")
                        )
                    )
                    if not consistent:
                        raise ValueError(f"conflicting authenticated package identity: {key}")
                selected[key] = record
    if set(binaries) != binary_keys or set(sources) != source_keys:
        raise ValueError("covered package is absent from authenticated indices")
    for name, version, source_name, source_version in covered:
        record = binaries[name.split(":")[0], version]
        declared = record.get("Source", name.split(":")[0]).split()
        declared_version = declared[1].strip("()") if len(declared) == 2 else version
        if (declared[0], declared_version) != (source_name, source_version):
            raise ValueError("binary/source identity differs from authenticated package metadata")
    source_inventory: list[dict[str, object]] = []
    for key, record in sorted(sources.items()):
        files = source_files(toolchain / "sources", record)
        descriptors = [item for item in files if str(item["name"]).endswith(".dsc")]
        if len(descriptors) != 1:
            raise ValueError("source package lacks its exact descriptor")
        descriptor = toolchain / "sources" / str(descriptors[0]["name"])
        fields = next(paragraphs(cleartext(descriptor.read_text(encoding="utf-8"))))
        if (fields["Source"], fields["Version"]) != key:
            raise ValueError("source descriptor identity differs from authenticated index")
        source_files(toolchain / "sources", fields)
        source_inventory.append({"name": key[0], "version": key[1], "files": files})
    debs: dict[tuple[str, str], Path] = {}
    for archive in sorted((toolchain / "debs").glob("*.deb")):
        values: list[str] = []
        for field in ("Package", "Version"):
            values.append(
                subprocess.run(  # noqa: S603 - fixed tool, collected package
                    ["/usr/bin/dpkg-deb", "--field", str(archive), field],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )
        debs[values[0], values[1]] = archive
    binary_inventory: list[dict[str, str | int]] = []
    for key, record in sorted(binaries.items()):
        archive = debs.get(key)
        if archive is None:
            raise ValueError("authenticated binary archive was not retained")
        verify_file(archive, (record["SHA256"], int(record["Size"])))
        binary_inventory.append(
            {
                "name": key[0],
                "version": key[1],
                "archive": archive.name,
                "sha256": record["SHA256"],
                "size": int(record["Size"]),
            }
        )
    output = evidence / "authenticated-toolchain.json"
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            {
                "schema_version": 1,
                "trust": "archive keyring from digest-pinned builder",
                "keyring_sha256": digest(keyring),
                "releases": authenticated_releases,
                "indices": indices,
                "sources": source_inventory,
                "binaries": binary_inventory,
                "descriptor_signature_scope": "descriptor hash authenticated by signed Sources",
                "snapshot_expiry": "historical Valid-Until disabled; signatures checked",
            },
            stream,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: authenticate.py TOOLCHAIN EVIDENCE")
    authenticate(Path(sys.argv[1]), Path(sys.argv[2]))
