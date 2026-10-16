"""Derive native component ownership from checked compile, link and install records."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from sobafm.native import (
    NativeComponent,
    NativeFile,
    Sha256,
    SourceArchive,
    Target,
    file_sha256,
    package_path,
)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourcePackage(_Record):
    name: str
    version: str
    files: list[SourceArchive]


class BinaryPackage(_Record):
    name: str
    version: str
    archive: str
    sha256: Sha256
    size: int


class Authentication(_Record):
    schema_version: Literal[1]
    trust: str
    keyring_sha256: Sha256
    releases: list[dict[str, str]]
    indices: list[SourceArchive]
    sources: list[SourcePackage]
    binaries: list[BinaryPackage]
    descriptor_signature_scope: str
    snapshot_expiry: str


class CompileUnit(_Record):
    object: str
    inputs: list[str]


class InstalledLink(_Record):
    path: str
    linked_sha256: Sha256
    installed_sha256: Sha256


class ArchiveMember(_Record):
    archive: str
    member: str


class LinkUnit(_Record):
    map: str
    map_sha256: Sha256
    outputs: list[str]
    inputs: list[str]
    archive_members: list[ArchiveMember]
    installed: InstalledLink


class InputMapping(_Record):
    schema_version: Literal[1]
    compile_units: list[CompileUnit]
    link_units: list[LinkUnit]


class RuntimeBinary(_Record):
    path: str
    sha256: Sha256
    size: int
    needed: list[str]
    runpath: list[str]
    glibc_symbol_versions: list[str]
    pe_security: dict[str, int]
    undefined_symbols: list[str] = Field(default_factory=list)


class RuntimeClosure(_Record):
    schema_version: Literal[1]
    target: Target
    binaries: list[RuntimeBinary]
    system_libraries: list[str]
    system_distribution: str


class ComponentRoute(_Record):
    license_expression: str
    route: str
    additional_sources: list[str] = Field(default_factory=list)
    required_archive_members: list[str] = Field(default_factory=list)


class FfmpegTerm(ComponentRoute):
    name: str
    input_paths: list[str]


class LicenseText(_Record):
    path: str
    first_line: int = Field(ge=1)
    last_line: int | None
    sha256: Sha256


class ComponentPolicy(_Record):
    schema_version: Literal[1]
    status: str
    scope: str
    source_packages: dict[str, ComponentRoute]
    target_source_packages: dict[Target, dict[str, ComponentRoute]] = Field(
        default_factory=dict[Target, dict[str, ComponentRoute]]
    )
    ffmpeg_terms: list[FfmpegTerm]
    license_ref_resolution: dict[str, str]
    license_ref_texts: dict[str, list[LicenseText]]


def selected_inputs(build: Path) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    for line in (build / "evidence/compiled-input-correspondence.tsv").read_text().splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or re.fullmatch("[0-9a-f]{64}", fields[0]) is None:
            raise ValueError("invalid selected native input record")
        checksum, path, origin = fields
        if path in records or not path.startswith(("/work/", "/usr/")):
            raise ValueError("ambiguous selected native input")
        records[path] = checksum, origin
    if not records:
        raise ValueError("selected native input inventory is empty")
    return records


def component_inventory(
    build: Path,
    toolchain: Path,
    files: list[NativeFile],
    upstream: SourceArchive,
    version: str,
    policy_path: Path,
    target: Target,
) -> tuple[list[NativeComponent], dict[str, object], list[str]]:
    policy = ComponentPolicy.model_validate_json(policy_path.read_bytes())
    authentication = Authentication.model_validate_json(
        (build / "evidence/authenticated-toolchain.json").read_bytes()
    )
    mapping = InputMapping.model_validate_json((build / "evidence/input-mapping.json").read_bytes())
    selected = selected_inputs(build)
    binary_files = {item.path: item for item in files if item.binary}
    closure = RuntimeClosure.model_validate_json(
        (build / "evidence/runtime-closure.json").read_bytes()
    )
    if closure.target != target or {item.path for item in closure.binaries} != binary_files.keys():
        raise ValueError("runtime closure does not match the packaged native target")
    if any(binary_files[item.path].sha256 != item.sha256 for item in closure.binaries):
        raise ValueError("runtime binary differs from its inspected dependency closure")
    if (
        len(mapping.link_units) != len(binary_files)
        or {unit.installed.path for unit in mapping.link_units} != binary_files.keys()
    ):
        raise ValueError("installed binary component mapping is incomplete")
    for unit in mapping.link_units:
        if binary_files[unit.installed.path].sha256 != unit.installed.installed_sha256:
            raise ValueError("installed binary differs from its checked link output")
    compile_units = {unit.object: unit.inputs for unit in mapping.compile_units}
    if len(compile_units) != len(mapping.compile_units):
        raise ValueError("compile-unit mapping has duplicate objects")
    related: dict[str, set[str]] = {}
    for unit in mapping.link_units:
        inputs = set(unit.inputs)
        for path in unit.inputs:
            if (
                path.startswith("/work/build/")
                and path.endswith(".o")
                and path not in compile_units
            ):
                raise ValueError("selected generated object lacks its compilation input mapping")
            inputs.update(compile_units.get(path, []))
        if not inputs.issubset(selected):
            raise ValueError("component mapping references an unverified selected input")
        if not {member.archive for member in unit.archive_members}.issubset(inputs):
            raise ValueError("archive member mapping lacks its selected linker input")
        related[unit.installed.path] = inputs
    upstream_paths = sorted(
        package_path(path.removeprefix("/work/source/"))
        for path, (_, origin) in selected.items()
        if origin == "ffmpeg-source"
    )
    if not upstream_paths:
        raise ValueError("FFmpeg selected source inventory is absent")
    notice_archive = "notices/selected-input-notices.tar.xz"
    components = [
        NativeComponent(
            name="ffmpeg",
            version=version,
            license_expression="LGPL-2.1-or-later",
            sources=[upstream],
            notices=["NOTICE.txt", "notices/ffmpeg/COPYING.LGPLv2.1", notice_archive],
            files=sorted(binary_files),
            distribution="runtime",
        )
    ]
    components.append(
        NativeComponent(
            name="os-system-libraries",
            version=target,
            license_expression="NOASSERTION",
            sources=[],
            notices=["NOTICE.txt"],
            files=[],
            distribution="system",
        )
    )
    assessments: list[dict[str, object]] = []
    for term in policy.ffmpeg_terms:
        sources = {"/work/source/" + package_path(path) for path in term.input_paths}
        owned_files = sorted(path for path, inputs in related.items() if sources & inputs)
        if not owned_files:
            continue
        components.append(
            NativeComponent(
                name=term.name,
                version=version,
                license_expression=term.license_expression,
                sources=[upstream],
                notices=[notice_archive],
                files=owned_files,
                distribution="incorporated-support",
            )
        )
        assessments.append(
            {
                "component": term.name,
                "route": term.route,
                "inputs": sorted(sources & selected.keys()),
                "files": owned_files,
            }
        )
    covered: dict[str, tuple[str, str]] = {}
    notices: dict[str, set[str]] = {}
    authenticated_binaries = {(item.name, item.version) for item in authentication.binaries}
    for line in (toolchain / "covered-packages.tsv").read_text().splitlines():
        row = line.split("\t")
        if len(row) != 4 or (row[0].split(":")[0], row[1]) not in authenticated_binaries:
            raise ValueError(
                "component package inventory lacks authenticated binary correspondence"
            )
        covered[row[0]] = row[2], row[3]
        notices.setdefault(row[2], set()).add(
            f"notices/toolchain/{package_path(row[0].split(':')[0])}/copyright"
        )
    source_by_name = {item.name: item for item in authentication.sources}
    overrides = policy.target_source_packages.get(target, {})
    if not overrides.keys() <= policy.source_packages.keys():
        raise ValueError("target license route overrides an unreviewed source package")
    routes = policy.source_packages | overrides
    for source in authentication.sources:
        for archive in source.files:
            material = toolchain / "sources" / archive.name
            if material.stat().st_size != archive.size or file_sha256(material) != archive.sha256:
                raise ValueError("corresponding source changed after authentication")
        route = routes.get(source.name)
        if route is None:
            raise ValueError(f"native source component lacks a reviewed route: {source.name}")
        source_owners = {
            name for name, pair in covered.items() if pair == (source.name, source.version)
        }
        member_files: dict[str, set[str]] = {}
        for unit in mapping.link_units:
            for member in unit.archive_members:
                if set(selected[member.archive][1].split(",")) & source_owners:
                    member_files.setdefault(member.member, set()).add(unit.installed.path)
        if not set(route.required_archive_members).issubset(member_files):
            raise ValueError("reviewed target license route lacks its selected archive members")
        owned: set[str] = set()
        observed: set[str] = set()
        for unit in mapping.link_units:
            member_archives = {member.archive for member in unit.archive_members}
            for path in related[unit.installed.path]:
                owners = set(selected[path][1].split(",")) & source_owners
                if not owners:
                    continue
                observed.add(path)
                if (path.endswith(".a") and path not in member_archives) or ".so" in Path(
                    path
                ).name:
                    continue
                owned.add(unit.installed.path)
        corresponding = list(source.files)
        component_notices = set(notices[source.name])
        extra_prefix = {
            "gcc-14": "gcc",
            "mingw-w64": "mingw",
            "glibc": "glibc",
            "linux": "linux",
        }.get(source.name)
        if extra_prefix:
            component_notices.update(
                file.path
                for file in files
                if file.path.startswith(f"notices/extra/{extra_prefix}/")
            )
        for dependency in route.additional_sources:
            additional = source_by_name.get(dependency)
            if additional is None:
                raise ValueError("cross-tool component lacks its underlying authenticated source")
            corresponding.extend(additional.files)
            component_notices.update(notices[dependency])
        components.append(
            NativeComponent(
                name="source-" + source.name,
                version=source.version,
                license_expression=route.license_expression,
                sources=corresponding,
                notices=sorted(component_notices),
                files=sorted(owned),
                distribution="incorporated-support" if owned else "build-only",
            )
        )
        assessments.append(
            {
                "component": "source-" + source.name,
                "route": route.route,
                "inputs": sorted(observed),
                "files": sorted(owned),
                "license_route_member_files": {
                    member: sorted(member_files[member])
                    for member in route.required_archive_members
                },
            }
        )
    return (
        components,
        {
            "schema_version": 1,
            "assessments": assessments,
            "mapping": "build-materials/evidence/input-mapping.json",
            "authentication": "build-materials/evidence/authenticated-toolchain.json",
            "source_notice_scope": "complete selected upstream source bytes accompany the runtime",
            "os_requirements": closure.model_dump(),
        },
        upstream_paths,
    )


def _external_references(component: NativeComponent) -> list[dict[str, str]]:
    if component.name == "ffmpeg":
        return [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": "pkg:generic/ffmpeg@" + quote(component.version, safe=""),
            },
            {
                "referenceCategory": "SECURITY",
                "referenceType": "cpe23Type",
                "referenceLocator": "cpe:2.3:a:ffmpeg:ffmpeg:"
                + component.version
                + ":*:*:*:*:*:*:*",
            },
        ]
    if component.name.startswith("source-"):
        return [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": "pkg:deb/debian/"
                + quote(component.name.removeprefix("source-"), safe="")
                + "@"
                + quote(component.version, safe="")
                + "?arch=source&distro=debian-13",
            }
        ]
    return []


def write_spdx(
    destination: Path,
    runtime: Path,
    components: list[NativeComponent],
    files: list[NativeFile],
    target: str,
    epoch: int,
    policy_path: Path,
    system_libraries: list[str],
) -> None:
    """Write a deterministic SPDX inventory; qualification is recorded separately."""
    package_ids = {
        component.name: f"SPDXRef-Component-{index}" for index, component in enumerate(components)
    }
    file_ids = {file.path: f"SPDXRef-File-{index}" for index, file in enumerate(files)}
    identity = hashlib.sha256(
        json.dumps(
            [component.model_dump() for component in components],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    packages: list[dict[str, object]] = []
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": package_ids["ffmpeg"],
        }
    ]
    policy = ComponentPolicy.model_validate_json(policy_path.read_bytes())
    custom: dict[str, str] = {}
    source_materials = {
        source.sha256: source for component in components for source in component.sources
    }
    for checksum, source in sorted(source_materials.items()):
        packages.append(
            {
                "SPDXID": "SPDXRef-Source-" + checksum,
                "name": source.name,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "checksums": [{"algorithm": "SHA256", "checksumValue": checksum}],
                "licenseDeclared": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "sourceInfo": (
                    "Exact source material accompanies the runtime in its paired source bundle."
                ),
                "comment": "Source archive/descriptor; retain its original per-file terms.",
            }
        )
    for component in components:
        packages.append(
            {
                "SPDXID": package_ids[component.name],
                "name": component.name,
                "versionInfo": component.version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": component.license_expression,
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "sourceInfo": "Selected contribution; complete corresponding sources: "
                + ", ".join(
                    f"{source.name} (SHA256 {source.sha256})" for source in component.sources
                ),
                "comment": "Distribution role: "
                + component.distribution
                + "; notices: "
                + ", ".join(component.notices)
                + (
                    "; OS-provided names: " + ", ".join(system_libraries)
                    if component.distribution == "system"
                    else ""
                ),
                "externalRefs": _external_references(component),
            }
        )
        for source in component.sources:
            relationships.append(
                {
                    "spdxElementId": package_ids[component.name],
                    "relationshipType": "GENERATED_FROM",
                    "relatedSpdxElement": "SPDXRef-Source-" + source.sha256,
                }
            )
        for path in component.files:
            relationships.append(
                {
                    "spdxElementId": package_ids[component.name],
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": file_ids[path],
                }
            )
        if component.name != "ffmpeg":
            relationships.append(
                {
                    "spdxElementId": package_ids[component.name]
                    if component.distribution == "build-only"
                    else package_ids["ffmpeg"],
                    "relationshipType": "BUILD_DEPENDENCY_OF"
                    if component.distribution == "build-only"
                    else "DEPENDS_ON",
                    "relatedSpdxElement": package_ids["ffmpeg"]
                    if component.distribution == "build-only"
                    else package_ids[component.name],
                }
            )
        for reference in re.findall(r"LicenseRef-[A-Za-z0-9.-]+", component.license_expression):
            texts = policy.license_ref_texts.get(reference)
            if not texts:
                raise ValueError("SPDX custom license lacks its exact accompanying text")
            extracted: list[str] = []
            for text in texts:
                content = (runtime / package_path(text.path)).read_bytes().decode("utf-8")
                fragment = "".join(
                    content.splitlines(keepends=True)[text.first_line - 1 : text.last_line]
                )
                if hashlib.sha256(fragment.encode()).hexdigest() != text.sha256:
                    raise ValueError("SPDX custom license text differs from its reviewed notice")
                extracted.append(fragment)
            custom[reference] = "\n\n".join(extracted)
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "SobaFM minimal FFmpeg " + target,
        "documentNamespace": "https://github.com/slackysoba/sobafm/native-sbom/" + identity,
        "creationInfo": {
            "creators": ["Tool: SobaFM native supply"],
            "created": datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "packages": packages,
        "files": [
            {
                "SPDXID": file_ids[file.path],
                "fileName": "./" + file.path,
                "checksums": [{"algorithm": "SHA256", "checksumValue": file.sha256}],
                "licenseConcluded": "NOASSERTION",
                "licenseInfoInFile": ["NOASSERTION"],
                "copyrightText": "NOASSERTION",
            }
            for file in files
        ],
        "relationships": relationships,
        "hasExtractedLicensingInfos": [
            {"licenseId": name, "extractedText": value} for name, value in sorted(custom.items())
        ],
    }
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
