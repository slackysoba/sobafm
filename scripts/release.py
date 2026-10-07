"""Validate release sources and plan moving aliases without publishing anything."""

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from packaging.version import InvalidVersion, Version

DOCKER_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
MAIN_REF = "refs/remotes/origin/main"


class ReleaseError(ValueError):
    """A release does not satisfy the accepted publication policy."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: Version
    commit: str = ""

    @property
    def image_tag(self) -> str:
        return self.tag[1:]

    @property
    def minor_tag(self) -> str:
        return f"{self.version.release[0]}.{self.version.release[1]}"


def parse_tag(tag: str) -> Release:
    if not tag.startswith("v"):
        raise ReleaseError("Release tags must start with v")
    value = tag[1:]
    if DOCKER_TAG.fullmatch(value) is None:
        raise ReleaseError("The release version must also be a valid Docker tag")
    try:
        version = Version(value)
    except InvalidVersion as error:
        raise ReleaseError("The release tag is not a PEP 440 version") from error
    if not value[0].isdigit() or len(version.release) != 3:
        raise ReleaseError("Release tags must contain exactly three numeric components: X.Y.Z")
    return Release(tag, version)


def project_version(text: str) -> Version:
    try:
        document = cast("dict[str, object]", tomllib.loads(text))
    except tomllib.TOMLDecodeError as error:
        raise ReleaseError("The tagged pyproject.toml is invalid TOML") from error
    project = document.get("project")
    if not isinstance(project, dict):
        raise ReleaseError("pyproject.toml must contain a project table")
    value = cast("dict[str, object]", project).get("version")
    if not isinstance(value, str):
        raise ReleaseError("project.version must be a string")
    try:
        return Version(value)
    except InvalidVersion as error:
        raise ReleaseError("project.version is not a PEP 440 version") from error


def git(repository: Path, *arguments: str, allow_non_ancestor: bool = False) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ReleaseError("Git is required to validate the release source")
    result = subprocess.run(  # noqa: S603 - fixed Git executable and argument list, no shell
        [executable, *arguments],
        cwd=repository,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if allow_non_ancestor and result.returncode == 1:
        raise ReleaseError("The release commit is not on origin/main")
    if result.returncode != 0:
        raise ReleaseError(f"Git could not validate the release source: {result.stderr.strip()}")
    return result.stdout.strip()


def tagged_release(repository: Path, tag: str) -> Release:
    release = parse_tag(tag)
    commit = git(repository, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    version = project_version(git(repository, "show", f"{commit}:pyproject.toml"))
    if release.version != version:
        raise ReleaseError("The release tag does not match the tagged project.version")
    return Release(tag, release.version, commit)


def validate_source(repository: Path, tag: str) -> Release:
    release = tagged_release(repository, tag)
    if release.commit != git(repository, "rev-parse", "HEAD"):
        raise ReleaseError("The checkout must be the tagged release commit")
    git(
        repository, "merge-base", "--is-ancestor", release.commit, MAIN_REF, allow_non_ancestor=True
    )
    return release


def stable_releases(repository: Path) -> list[Release]:
    # Protected Git tags retain promotion history even if release-note creation failed.
    tags = git(repository, "tag", "--list", "v*", "--merged", MAIN_REF).splitlines()
    releases: list[Release] = []
    for tag in tags:
        try:
            release = tagged_release(repository, tag)
        except ReleaseError:
            continue  # Invalid release tags never participate in alias ordering.
        if not release.version.is_prerelease:
            releases.append(release)
    return releases


def aliases(release: Release, releases: Iterable[Release]) -> tuple[str, ...]:
    if release.version.is_prerelease:
        return ()
    stable = [other for other in releases if not other.version.is_prerelease]
    result: list[str] = []
    minor = [other.version for other in stable if other.minor_tag == release.minor_tag]
    if release.version >= max(minor, default=release.version):
        result.append(release.minor_tag)
    if release.version >= max((other.version for other in stable), default=release.version):
        result.append("latest")
    return tuple(result)


def outputs(release: Release, promoted: tuple[str, ...] = ()) -> dict[str, str]:
    return {
        "image_tag": release.image_tag,
        "normalized_version": str(release.version),
        "minor_tag": release.minor_tag,
        "prerelease": str(release.version.is_prerelease).lower(),
        "source_sha": release.commit,
        "promote_minor": str(release.minor_tag in promoted).lower(),
        "promote_latest": str("latest" in promoted).lower(),
    }


@dataclass
class Arguments(argparse.Namespace):
    mode: str = ""
    tag: str = ""
    repository: Path = Path()
    output: Path | None = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "promote"))
    parser.add_argument("tag")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        release = validate_source(args.repository, args.tag)
        promoted = (
            aliases(release, stable_releases(args.repository)) if args.mode == "promote" else ()
        )
        content = "".join(f"{key}={value}\n" for key, value in outputs(release, promoted).items())
        if args.output is None:
            print(content, end="")
        else:
            with args.output.open("a", encoding="utf-8", newline="\n") as output:
                output.write(content)
    except (ReleaseError, OSError) as error:
        print(f"Release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
