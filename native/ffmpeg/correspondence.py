"""Record selected GNU compiler/linker inputs, refusing unresolved correspondence.

This build-only helper uses the builder's existing Python standard library.
It does not establish source-package authentication or a complete license/SBOM.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

_VIRTUAL_COMPILER = {"<built-in>", "<command-line>"}
_MEMBER = re.compile(r"[A-Za-z0-9_.$+-]+")
_ARCHIVE_REF = re.compile(r"([^\s()]+\.a)\(([^\s()]+)\)")
_ABSOLUTE_REF = re.compile(r'(?<![^\s(])/[^\s();,"]+')


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dependency_rules(text: str) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Accept compiler .d rules; reject syntax this fixed-path recipe cannot map."""
    rules: dict[str, set[str]] = {}
    scaffolding: set[tuple[str, str]] = set()
    for line in text.replace("\\\n", " ").splitlines():
        if not line.strip():
            continue
        target, separator, dependencies = line.partition(":")
        if (
            not separator
            or not target.endswith(".o")
            or any(char.isspace() or char in "$#\\" for char in target)
            or target in rules
        ):
            raise ValueError("unsupported compiler dependency rule")
        inputs: set[str] = set()
        for token in shlex.split(dependencies, comments=False, posix=True):
            if token in _VIRTUAL_COMPILER:
                scaffolding.add(("compiler-virtual", token))
            elif not token or any(char.isspace() or char in "$#\\:" for char in token):
                raise ValueError("unsupported compiler prerequisite path")
            else:
                inputs.add(token)
        rules[target] = inputs
    return rules, scaffolding


def prerequisites(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    rules, scaffolding = dependency_rules(text)
    return {path for inputs in rules.values() for path in inputs}, scaffolding


def linker_inputs(text: str) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    """Keep real LOAD/archive/absolute inputs apart from explicit GNU ld scaffolding."""
    inputs: set[str] = set()
    members: set[tuple[str, str]] = set()
    scaffolding: set[tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "LOAD dll stuff":
            # GNU ld pe-dll.c creates this exact fake input for generated thunks.
            scaffolding.add(("linker-generated-import-thunk", "dll stuff"))
            continue
        if stripped.startswith("SEARCH_DIR("):
            directories = re.findall(r'SEARCH_DIR\("=?([^"\n]+)"\);?', stripped)
            remainder = re.sub(r'SEARCH_DIR\("=?[^"\n]+"\);?', "", stripped)
            if not directories or remainder.strip():
                raise ValueError("unsupported linker search-directory record")
            scaffolding.update(("linker-search-directory", value) for value in directories)
            continue
        if stripped.startswith("OUTPUT("):
            match = re.fullmatch(r"OUTPUT\(([^\s()]+) [A-Za-z0-9_-]+\)", stripped)
            if match is None:
                raise ValueError("unsupported linker output record")
            scaffolding.add(("linker-output", match[1]))
            continue
        if stripped.startswith("LOAD "):
            match = re.fullmatch(r"LOAD ([^\s()]+)", stripped)
            if match is None:
                raise ValueError("unsupported linker LOAD record")
            inputs.add(match[1])
        for archive, member in _ARCHIVE_REF.findall(line):
            inputs.add(archive)
            members.add((archive, member))
        for reference in _ABSOLUTE_REF.findall(line):
            if reference == "/DISCARD/":
                scaffolding.add(("linker-virtual-section", reference))
            else:
                inputs.add(reference)
    return inputs, members, scaffolding


def resolve_input(token: str, build: Path) -> Path:
    path = Path(token)
    if not path.is_absolute():
        path = build / path
    try:
        actual = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"unresolved selected input: {token}") from error
    if not actual.is_file():
        raise ValueError(f"selected input is not a regular file: {token}")
    return actual


def extract_member(archive: Path, member: str, destination: Path) -> str:
    """Check both member selection and ar completion; retain failed output bytes."""
    if not archive.is_file() or _MEMBER.fullmatch(member) is None:
        raise ValueError("invalid selected archive or member")
    listing = subprocess.run(  # noqa: S603 - selected builder archive, fixed pinned tool
        ["/usr/bin/ar", "t", str(archive)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    if listing.count(member) != 1:
        raise ValueError("selected archive member is absent or ambiguous")
    with destination.open("xb") as output:
        subprocess.run(  # noqa: S603 - validated member from selected builder archive
            ["/usr/bin/ar", "p", str(archive), member],
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
        )
    if destination.stat().st_size == 0:
        raise ValueError("selected archive member is empty")
    return sha256(destination)


def write_new(path: Path, lines: list[str]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.writelines(lines)


def verify_upstream_inputs(
    actual: list[Path], source: Path, archive_path: Path, version: str
) -> None:
    wanted = {
        f"ffmpeg-{version}/{path.relative_to(source).as_posix()}": sha256(path)
        for path in actual
        if path.is_relative_to(source)
    }
    observed: set[str] = set()
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            if member.name not in wanted:
                continue
            if member.name in observed or not member.isfile():
                raise ValueError("selected source input is ambiguous in the upstream archive")
            content = archive.extractfile(member)
            if content is None:
                raise ValueError("selected source archive input is unreadable")
            with content:
                checksum = hashlib.sha256()
                while chunk := content.read(1024 * 1024):
                    checksum.update(chunk)
                if checksum.hexdigest() != wanted[member.name]:
                    raise ValueError(
                        "selected source input differs from the pinned upstream archive"
                    )
            observed.add(member.name)
    if observed != wanted.keys():
        raise ValueError("selected source input is absent from the upstream archive")


def collect(
    work: Path,
    toolchain: Path,
    source_archive: Path,
    target: str,
    result_directory: Path | None = None,
) -> None:
    build = work / "build"
    evidence = work / "evidence"
    results = evidence if result_directory is None else result_directory
    if result_directory is not None:
        results.mkdir()
    dependency_files = sorted(build.rglob("*.d"))
    maps = sorted((evidence / "links").glob("*.map"))
    if not dependency_files or not maps:
        raise ValueError("actual compiler dependencies and link maps are required")
    inputs: set[str] = set()
    members: set[tuple[str, str]] = set()
    scaffolding: set[tuple[str, str]] = set()
    compile_units: dict[str, set[str]] = {}
    link_units: list[tuple[Path, set[str], set[tuple[str, str]], set[tuple[str, str]]]] = []
    for path in dependency_files:
        rules, classified = dependency_rules(path.read_text(encoding="utf-8"))
        for object_target, dependencies in rules.items():
            if object_target in compile_units and compile_units[object_target] != dependencies:
                raise ValueError("compiler target has conflicting dependency records")
            compile_units[object_target] = dependencies
            inputs.update(dependencies)
            inputs.add(object_target)
        scaffolding.update(classified)
    for path in maps:
        selected, selected_members, classified = linker_inputs(path.read_text(encoding="utf-8"))
        inputs.update(selected)
        members.update(selected_members)
        scaffolding.update(classified)
        link_units.append((path, selected, selected_members, classified))
    if not inputs:
        raise ValueError("actual compiler/linker input inventory is empty")
    for kind, token in scaffolding:
        if kind == "linker-output":
            output = resolve_input(token, build)
            if not output.is_relative_to(build.resolve(strict=True)):
                raise ValueError("linker output is outside the retained build")
    covered = {
        line.split("\t")[0]
        for line in (toolchain / "covered-packages.tsv").read_text(encoding="utf-8").splitlines()
    }
    actual_inputs = sorted({resolve_input(token, build) for token in inputs})
    pin = json.loads((work / "recipe/source.json").read_text(encoding="utf-8"))
    if sha256(source_archive) != pin["archive_sha256"]:
        raise ValueError("correspondence source archive identity mismatch")
    verify_upstream_inputs(actual_inputs, work / "source", source_archive, pin["version"])
    records: list[str] = []
    for actual in actual_inputs:
        if actual.is_relative_to((work / "source").resolve(strict=True)):
            origin = "ffmpeg-source"
        elif actual.is_relative_to(build.resolve(strict=True)):
            origin = "retained-generated-build"
        else:
            result = subprocess.run(  # noqa: S603 - resolved build input, fixed pinned tool
                ["/usr/bin/dpkg-query", "-S", str(actual)],
                capture_output=True,
                text=True,
                check=True,
            )
            owners = {line.rsplit(": ", 1)[0] for line in result.stdout.splitlines()}
            if not owners or not owners.issubset(covered):
                raise ValueError(
                    f"selected input lacks a covered package owner: {actual}: {sorted(owners)}"
                )
            packaged = toolchain / "package-files" / str(actual).lstrip("/")
            if not packaged.is_file() or sha256(actual) != sha256(packaged):
                raise ValueError("selected input differs from collected package bytes")
            origin = ",".join(sorted(owners))
        records.append(f"{sha256(actual)}\t{actual}\t{origin}\n")
    member_directory = results / "archive-members"
    member_directory.mkdir()
    selections: list[str] = []
    for token, member in sorted(members):
        archive = resolve_input(token, build)
        name = hashlib.sha256(f"{archive}({member})".encode()).hexdigest()
        digest = extract_member(archive, member, member_directory / f"{name}.o")
        selections.append(f"{digest}\t{archive}\t{member}\n")
    write_new(results / "compiled-input-correspondence.tsv", records)
    write_new(results / "selected-archive-members.tsv", selections)
    write_new(
        results / "classified-scaffolding.tsv",
        [f"{kind}\t{token}\n" for kind, token in sorted(scaffolding)],
    )
    prefix = work / "prefix/opt/sobafm-native"
    installed = {
        sha256(path): path.relative_to(prefix).as_posix()
        for directory, pattern in ((prefix / "bin", "*"), (prefix / "lib", "*.so*"))
        for path in directory.glob(pattern)
        if path.is_file()
        and not path.is_symlink()
        and (target != "windows-amd64" or path.suffix in {".exe", ".dll"})
    }
    stripped = results / "stripped-link-outputs"
    stripped.mkdir()
    strip_tool = (
        "/usr/bin/x86_64-w64-mingw32-strip" if target == "windows-amd64" else "/usr/bin/strip"
    )
    install_mapping: dict[str, dict[str, str]] = {}
    for path, _, _, classified in link_units:
        outputs = [value for kind, value in classified if kind == "linker-output"]
        if len(outputs) != 1:
            raise ValueError("runtime link map must identify one concrete output")
        linked = resolve_input(outputs[0], build)
        destination = stripped / path.name
        subprocess.run(  # noqa: S603 - own link output, fixed pinned stripping tool
            [strip_tool, "-o", str(destination), str(linked)],
            capture_output=True,
            check=True,
        )
        stripped_digest = sha256(destination)
        runtime_path = installed.get(stripped_digest)
        if runtime_path is None:
            raise ValueError("stripped link output does not match an installed runtime binary")
        install_mapping[path.name] = {
            "path": runtime_path,
            "linked_sha256": sha256(linked),
            "installed_sha256": stripped_digest,
        }
    if {record["path"] for record in install_mapping.values()} != set(installed.values()):
        raise ValueError("installed runtime is not fully bound to actual link outputs")
    mapping = {
        "schema_version": 1,
        "compile_units": [
            {
                "object": str(resolve_input(target, build)),
                "inputs": sorted(str(resolve_input(item, build)) for item in dependencies),
            }
            for target, dependencies in sorted(compile_units.items())
        ],
        "link_units": [
            {
                "map": path.name,
                "map_sha256": sha256(path),
                "installed": install_mapping[path.name],
                "outputs": sorted(
                    str(resolve_input(value, build))
                    for kind, value in classified
                    if kind == "linker-output"
                ),
                "inputs": sorted({str(resolve_input(item, build)) for item in selected}),
                "archive_members": [
                    {"archive": str(resolve_input(archive, build)), "member": member}
                    for archive, member in sorted(selected_members)
                ],
            }
            for path, selected, selected_members, classified in link_units
        ],
    }
    with (results / "input-mapping.json").open("x", encoding="utf-8", newline="\n") as output:
        json.dump(mapping, output, indent=2, sort_keys=True)
        output.write("\n")
    compiler_inputs = {
        resolve_input(item, build)
        for dependencies in compile_units.values()
        for item in dependencies
    }
    notice_root = results / "selected-input-notices"
    notice_root.mkdir()
    notice_index: list[dict[str, str]] = []
    for actual in sorted(compiler_inputs):
        relative = actual.relative_to("/").as_posix()
        destination = notice_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(actual, destination)
        notice_index.append({"input": str(actual), "entry": relative, "sha256": sha256(actual)})
    with (notice_root / "index.json").open("x", encoding="utf-8", newline="\n") as output:
        json.dump({"schema_version": 1, "files": notice_index}, output, indent=2, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    if len(sys.argv) not in {5, 6}:
        raise SystemExit(
            "usage: correspondence.py WORK TOOLCHAIN UPSTREAM_ARCHIVE TARGET [RESULTS]"
        )
    collect(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Path(sys.argv[3]),
        sys.argv[4],
        Path(sys.argv[5]) if len(sys.argv) == 6 else None,
    )
