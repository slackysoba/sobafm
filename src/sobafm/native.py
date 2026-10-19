"""Verify a separately supplied native runtime against a trusted package identity.

This verifies supply, not containment, audio accounting or catalog readiness. Callers
must obtain the expected manifest digest from reviewed distribution metadata; a
manifest supplied alongside an arbitrary executable is not a trust anchor.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import struct
from importlib.resources import files as resource_files
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Target = Literal["linux-amd64", "linux-arm64", "windows-amd64"]
_MAX_MANIFEST_BYTES = 256 * 1024


class NativeSupplyError(ValueError):
    """A package cannot establish the requested native supply identity."""


def package_path(value: str) -> str:
    """Require a portable, unambiguous path inside a native package."""
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
        or any(char in value for char in '<>"|?*')
        or any(
            part.endswith((" ", "."))
            or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".")[0])
            for part in path.parts
        )
    ):
        raise ValueError("native package paths must be relative and canonical")
    return value


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NativeFile(_Record):
    path: str
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    binary: bool = False

    _path = field_validator("path")(package_path)


class NativeLink(_Record):
    path: str
    target: str

    _paths = field_validator("path", "target")(package_path)


class SourceArchive(_Record):
    name: str
    sha256: Sha256
    size: Annotated[int, Field(gt=0)]

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        if "/" in package_path(value):
            raise ValueError("source archive name must be a single filename")
        return value


class NativeComponent(_Record):
    name: str
    version: str
    license_expression: str
    sources: list[SourceArchive]
    notices: list[str]
    files: list[str] = Field(default_factory=list)
    distribution: Literal["runtime", "incorporated-support", "build-only", "system"]

    @field_validator("notices")
    @classmethod
    def notice_paths(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("each component needs its applicable notices")
        return [package_path(value) for value in values]

    @field_validator("files")
    @classmethod
    def file_paths(cls, values: list[str]) -> list[str]:
        return [package_path(value) for value in values]


class NativeManifest(_Record):
    schema_version: Literal[1]
    target: Target
    ffmpeg_version: str
    upstream_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    source: SourceArchive
    source_bundle: SourceArchive
    recipe_sha256: Sha256
    toolchain_sha256: Sha256
    configure: list[str]
    executable: str
    files: list[NativeFile]
    links: list[NativeLink]
    components: list[NativeComponent]

    _executable = field_validator("executable")(package_path)


class VerifiedNativeRuntime(_Record):
    """Supply identity only; the caller still has to qualify safe execution."""

    executable: Path
    manifest_sha256: Sha256
    target: Target
    source_bundle: SourceArchive


class ApprovedNativeIdentity(_Record):
    target: Target
    manifest_sha256: Sha256
    source_bundle_sha256: Sha256
    approval_record: Annotated[str, Field(min_length=1)]


class NativeTrustRegistry(_Record):
    schema_version: Literal[1]
    approvals: Annotated[list[ApprovedNativeIdentity], Field(max_length=128)]
    revoked_manifest_sha256: list[Sha256]


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native_target() -> Target:
    machine = platform.machine().casefold()
    if os.name == "nt" and machine in {"amd64", "x86_64"}:
        return "windows-amd64"
    if platform.system() == "Linux":
        if machine in {"x86_64", "amd64"}:
            return "linux-amd64"
        if machine in {"aarch64", "arm64"}:
            return "linux-arm64"
    raise NativeSupplyError("native runtime is unsupported on this platform")


def _machine(path: Path, target: Target) -> None:
    # Only the bounded architecture headers are needed here. Full library/import
    # inspection belongs to packaging, where the platform inspection tools exist.
    with path.open("rb") as stream:
        header = stream.read(64)
        if target.startswith("linux-"):
            expected = 62 if target == "linux-amd64" else 183
            if (
                len(header) != 64
                or header[:6] != b"\x7fELF\x02\x01"
                or struct.unpack_from("<H", header, 18)[0] != expected
            ):
                raise NativeSupplyError("native file has the wrong ELF architecture")
        else:
            if len(header) != 64 or header[:2] != b"MZ":
                raise NativeSupplyError("native file has no PE header")
            offset = struct.unpack_from("<I", header, 60)[0]
            if not 64 <= offset <= 4096:
                raise NativeSupplyError("native file has an invalid PE header offset")
            stream.seek(offset)
            pe = stream.read(6)
            if pe != b"PE\x00\x00\x64\x86":
                raise NativeSupplyError("native file has the wrong PE architecture")


def _policy(manifest: NativeManifest) -> None:
    required = {
        "--disable-all",
        "--disable-autodetect",
        "--disable-network",
        "--disable-iconv",
        "--disable-gpl",
        "--disable-nonfree",
        "--disable-version3",
        "--disable-static",
        "--enable-shared",
        "--enable-decoder=mp3float",
        "--enable-parser=mpegaudio",
        "--enable-demuxer=mp3",
        "--enable-encoder=pcm_s16le",
        "--enable-muxer=pcm_s16le",
        "--enable-protocol=pipe",
    }
    enabled = {
        "--enable-shared",
        "--enable-avutil",
        "--enable-avcodec",
        "--enable-avformat",
        "--enable-avfilter",
        "--enable-swresample",
        "--enable-ffmpeg",
        "--enable-decoder=mp3float",
        "--enable-parser=mpegaudio",
        "--enable-demuxer=mp3",
        "--enable-encoder=pcm_s16le",
        "--enable-muxer=pcm_s16le",
        "--enable-protocol=pipe",
        "--enable-filter=aresample",
        "--enable-cross-compile",
        "--enable-w32threads",
    }
    if not required.issubset(manifest.configure) or any(
        flag.startswith("--enable-") and flag not in enabled for flag in manifest.configure
    ):
        raise NativeSupplyError("native build is outside the reviewed minimal shared route")
    components = {item.name: item for item in manifest.components}
    ffmpeg = components.get("ffmpeg")
    if ffmpeg is None or ffmpeg.license_expression != "LGPL-2.1-or-later":
        raise NativeSupplyError("native build lacks the reviewed FFmpeg license identity")
    if not manifest.components or any(
        not item.sources for item in manifest.components if item.distribution != "system"
    ):
        raise NativeSupplyError("native component corresponding source is incomplete")
    if any(item.files for item in manifest.components if item.distribution == "system"):
        raise NativeSupplyError("system requirements cannot own redistributed native bytes")
    paths = [item.path for item in manifest.files] + [item.path for item in manifest.links]
    if len({path.casefold() for path in paths}) != len(paths):
        raise NativeSupplyError("native manifest contains duplicate or ambiguous paths")
    if manifest.executable not in {item.path for item in manifest.files if item.binary}:
        raise NativeSupplyError("native executable is absent from the binary inventory")
    notice_paths = {item.path for item in manifest.files}
    if any(path not in notice_paths for item in manifest.components for path in item.notices):
        raise NativeSupplyError("native component notices are absent from the package")
    owned_binaries = {path for item in manifest.components for path in item.files}
    if owned_binaries != {item.path for item in manifest.files if item.binary}:
        raise NativeSupplyError("native binary component inventory is incomplete")


def _verify_native_runtime(
    root: Path, expected_manifest_sha256: str, *, target: Target | None = None
) -> VerifiedNativeRuntime:
    """Check every installed byte against an independently trusted manifest digest.

    No package executable is run. The explicit target is for cross-platform package
    inspection; production callers omit it to require their actual native platform.
    """
    if not root.is_absolute() or root.is_symlink() or root.is_junction() or not root.is_dir():
        raise NativeSupplyError("select an absolute installed native package directory")
    if re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None:
        raise NativeSupplyError("a trusted native manifest SHA-256 is required")
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES
    ):
        raise NativeSupplyError("native manifest is missing or exceeds its size limit")
    content = manifest_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_manifest_sha256:
        raise NativeSupplyError("native manifest does not match the trusted supply identity")
    try:
        manifest = NativeManifest.model_validate_json(content)
    except ValidationError as error:
        raise NativeSupplyError("native manifest is invalid") from error
    actual_target = native_target() if target is None else target
    if manifest.target != actual_target:
        raise NativeSupplyError("native package does not match the requested platform")
    _policy(manifest)
    expected = {"manifest.json"} | {item.path for item in manifest.files + manifest.links}
    found: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        if entry.is_junction():
            raise NativeSupplyError("native package contains a directory junction")
        if entry.is_symlink() or entry.is_file():
            found.add(relative)
        elif not entry.is_dir():
            raise NativeSupplyError("native package contains an unsupported file type")
    if found != expected:
        raise NativeSupplyError("native package contains missing or unexpected files")
    for item in manifest.files:
        path = root / item.path
        if any(parent.is_symlink() for parent in path.parents if parent != root):
            raise NativeSupplyError("native file has a symlinked parent")
        if path.is_symlink() or not path.is_file():
            raise NativeSupplyError("native file is absent or replaced by a link")
        if path.stat().st_mode & (stat.S_ISUID | stat.S_ISGID):
            raise NativeSupplyError("native package files cannot confer setuid/setgid privileges")
        if not path.resolve(strict=True).is_relative_to(root):
            raise NativeSupplyError("native file escapes the installed package")
        if path.stat().st_size != item.size or file_sha256(path) != item.sha256:
            raise NativeSupplyError("native file does not match its recorded digest")
        if item.binary:
            _machine(path, actual_target)
    for link in manifest.links:
        path = root / link.path
        if not path.is_symlink() or path.readlink().as_posix() != link.target:
            raise NativeSupplyError("native library link does not match its recorded target")
        if not path.resolve(strict=True).is_relative_to(root):
            raise NativeSupplyError("native library link escapes the installed package")
        if not path.resolve(strict=True).is_file():
            raise NativeSupplyError("native library link does not name a regular file")
    executable = root / manifest.executable
    if actual_target != "windows-amd64" and not os.access(executable, os.X_OK):
        raise NativeSupplyError("native executable lacks execute permission")
    return VerifiedNativeRuntime(
        executable=executable,
        manifest_sha256=expected_manifest_sha256,
        target=actual_target,
        source_bundle=manifest.source_bundle,
    )


def verify_native_runtime(
    root: Path, expected_manifest_sha256: str, *, target: Target | None = None
) -> VerifiedNativeRuntime:
    """Verify supply identity; a successful result does not enable catalog playback."""
    try:
        return _verify_native_runtime(root, expected_manifest_sha256, target=target)
    except OSError as error:
        raise NativeSupplyError("native package files could not be read safely") from error


def verify_approved_native_runtime(
    root: Path, *, registry: Path | None = None
) -> VerifiedNativeRuntime:
    """Require reviewed application/operator metadata kept outside the native package.

    An explicit registry is operator-controlled trust configuration, including for
    reviewed recipient replacements. A package's own index cannot approve itself.
    This establishes supply identity only and never enables catalog playback.
    """
    try:
        if registry is None:
            content = resource_files("sobafm").joinpath("native-trust.json").read_bytes()
        else:
            if (
                not registry.is_absolute()
                or registry.is_symlink()
                or registry.is_junction()
                or not registry.is_file()
                or registry.stat().st_size > _MAX_MANIFEST_BYTES
                or registry.resolve(strict=True).is_relative_to(root.resolve(strict=True))
            ):
                raise NativeSupplyError("trusted native registry must be outside the package")
            content = registry.read_bytes()
        trust = NativeTrustRegistry.model_validate_json(content)
    except (OSError, ValidationError) as error:
        raise NativeSupplyError("reviewed native trust metadata is missing or invalid") from error
    target = native_target()
    candidates = [
        identity
        for identity in trust.approvals
        if identity.target == target
        and identity.manifest_sha256 not in trust.revoked_manifest_sha256
    ]
    for identity in candidates:
        try:
            verified = verify_native_runtime(root, identity.manifest_sha256)
        except NativeSupplyError:
            continue
        if verified.source_bundle.sha256 != identity.source_bundle_sha256:
            raise NativeSupplyError("approved native source bundle identity is inconsistent")
        return verified
    raise NativeSupplyError(
        "install a native runtime with a current independently reviewed identity"
    )
