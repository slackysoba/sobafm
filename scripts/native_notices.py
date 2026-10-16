"""Preserve exact additional support notices from authenticated source materials."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sobafm.native import Sha256, Target, file_sha256, package_path

if __package__:
    from .native_inventory import Authentication
else:
    from native_inventory import Authentication


class NoticeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    sha256: Sha256
    size: int = Field(gt=0, le=1024 * 1024)
    targets: list[Target]
    archive: str | None = None
    archive_sha256: Sha256 | None = None
    member: str | None = None
    selected: str | None = None
    nested: str | None = None
    nested_archive_sha256: Sha256 | None = None
    nested_archive_size: int | None = Field(default=None, gt=0, le=512 * 1024 * 1024)

    _path = field_validator("path")(package_path)


class NoticeInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int
    files: list[NoticeInput]


def collect_support_notices(
    specification: Path, build: Path, toolchain: Path, destination: Path, target: Target
) -> None:
    """Copy complete reviewed files, never abbreviated or substitute license text."""
    spec = NoticeInputs.model_validate_json(specification.read_bytes())
    records = [record for record in spec.files if target in record.targets]
    if len({record.path.casefold() for record in records}) != len(records):
        raise ValueError("additional notice paths are ambiguous")
    authentication = Authentication.model_validate_json(
        (build / "evidence/authenticated-toolchain.json").read_bytes()
    )
    archives = {item.name: item for source in authentication.sources for item in source.files}
    destination.mkdir()
    by_archive: dict[tuple[str, str | None], list[NoticeInput]] = {}
    for record in records:
        if record.selected is not None:
            if any(
                value is not None
                for value in (
                    record.archive,
                    record.member,
                    record.nested,
                    record.nested_archive_sha256,
                    record.nested_archive_size,
                )
            ):
                raise ValueError("notice input has ambiguous origins")
            relative = package_path(record.selected.removeprefix("/"))
            source = build / "evidence/selected-input-notices" / relative
            _check(source, record)
            output = destination / record.path
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        elif record.archive is not None and record.member is not None:
            package_path(record.member)
            if record.nested is not None:
                package_path(record.nested)
                if record.nested_archive_sha256 is None or record.nested_archive_size is None:
                    raise ValueError("nested notice source archive lacks its reviewed identity")
            elif record.nested_archive_sha256 is not None or record.nested_archive_size is not None:
                raise ValueError("notice source has identity metadata without a nested archive")
            if package_path(record.archive) not in archives:
                raise ValueError("additional notice lacks authenticated corresponding source")
            if record.archive_sha256 != archives[record.archive].sha256:
                raise ValueError("additional notice source differs from the reviewed archive")
            by_archive.setdefault((record.archive, record.nested), []).append(record)
        else:
            raise ValueError("additional notice lacks its exact source origin")
    for (name, nested), wanted in by_archive.items():
        material = toolchain / "sources" / name
        identity = archives[name]
        if material.stat().st_size != identity.size or file_sha256(material) != identity.sha256:
            raise ValueError("additional notice source archive identity changed")
        # BZ2File handles concatenated bzip2 streams in Debian originals. The
        # tarfile stream adapter can stop at the first compressed-stream end.
        with tarfile.open(material, "r:*") as archive:
            if nested is None:
                _copy_members(archive, wanted, destination)
            else:
                try:
                    inner = archive.getmember(nested)
                except KeyError as error:
                    raise ValueError("nested notice source archive member is missing") from error
                if not inner.isfile():
                    raise ValueError("nested notice source archive is not a regular file")
                identities = {
                    (record.nested_archive_sha256, record.nested_archive_size) for record in wanted
                }
                if len(identities) != 1:
                    raise ValueError("nested notice source archive has ambiguous identities")
                checksum, size = identities.pop()
                if inner.size != size:
                    raise ValueError("nested notice source archive differs from reviewed identity")
                source = archive.extractfile(inner)
                if source is None:
                    raise ValueError("nested notice source archive is unreadable")
                # Authenticate the inner bytes in place without unpacking paths.
                with source:
                    digest = hashlib.sha256()
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                    if digest.hexdigest() != checksum:
                        raise ValueError("nested notice archive differs from reviewed identity")
                    source.seek(0)
                    with tarfile.open(fileobj=source, mode="r:*") as inner_archive:
                        _copy_members(inner_archive, wanted, destination)
    (destination / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    record.model_dump(
                        exclude={"nested", "nested_archive_sha256", "nested_archive_size"}
                        if record.nested is None
                        else set()
                    )
                    for record in records
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _copy_members(archive: tarfile.TarFile, wanted: list[NoticeInput], destination: Path) -> None:
    remaining = {record.member: record for record in wanted}
    if len(remaining) != len(wanted):
        raise ValueError("additional notice source members are ambiguous")
    for member in archive:
        record = remaining.get(member.name)
        if record is None:
            continue
        if not member.isfile() or member.size != record.size:
            raise ValueError("additional notice source member is not the expected file")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("additional notice source member is unreadable")
        output = destination / record.path
        output.parent.mkdir(parents=True, exist_ok=True)
        with source, output.open("xb") as stream:
            shutil.copyfileobj(source, stream)
        _check(output, record)
        del remaining[member.name]
        if not remaining:
            break
    if remaining:
        raise ValueError("additional notice source member is missing")


def _check(path: Path, record: NoticeInput) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size != record.size
        or file_sha256(path) != record.sha256
    ):
        raise ValueError("additional accompanying notice does not match its reviewed identity")
