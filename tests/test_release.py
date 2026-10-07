"""Offline checks for release inputs, source history, and moving alias safety."""

from pathlib import Path

import pytest
from packaging.version import Version

from scripts import release


@pytest.mark.parametrize(
    ("tag", "normalized", "prerelease"),
    [
        ("v1.2.3", "1.2.3", False),
        ("v1.0.0-rc.1", "1.0.0rc1", True),
        ("v1.0.0RC1", "1.0.0rc1", True),
        ("v1.0.0.dev2", "1.0.0.dev2", True),
        ("v1.0.0.post1", "1.0.0.post1", False),
        ("v01.002.0003", "1.2.3", False),
    ],
)
def test_accepts_docker_safe_pep440_versions(tag: str, normalized: str, prerelease: bool) -> None:
    parsed = release.parse_tag(tag)
    assert str(parsed.version) == normalized
    assert parsed.version.is_prerelease is prerelease
    assert parsed.image_tag == tag[1:]


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v",
        "vv1.2.3",
        "vlatest",
        "v1",
        "v1.2",
        "v1.2.3.4",
        "v1!1.2.3",
        "v1.2.3+local",
        "v1.2.3/other",
        "v1.2.3:other",
        "v1.2.3\nlatest=true",
        "v 1.2.3",
        "v1.2.3 ",
        "v1.2.3-" + "a" * 125,
    ],
)
def test_rejects_unsafe_or_alias_colliding_tags(tag: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.parse_tag(tag)


@pytest.mark.parametrize(
    "text",
    ["", "[project\n", "[project]\n", "[project]\nversion=1", '[project]\nversion="wrong"'],
)
def test_rejects_invalid_project_metadata(text: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.project_version(text)


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path
        release.git(path, "init", "--initial-branch=main")
        self.commit("0.1.0")
        self.update_main()

    def commit(self, version: str) -> str:
        (self.path / "pyproject.toml").write_text(
            f'[project]\nversion = "{version}"\n', encoding="utf-8"
        )
        release.git(self.path, "add", "pyproject.toml")
        release.git(
            self.path,
            "-c",
            "user.name=Release test",
            "-c",
            "user.email=release@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "Release fixture",
        )
        return release.git(self.path, "rev-parse", "HEAD")

    def update_main(self) -> None:
        release.git(self.path, "update-ref", release.MAIN_REF, "HEAD")

    def tag(self, tag: str, *, annotated: bool = False) -> None:
        args = ("-a", "-m", "Release fixture") if annotated else ()
        release.git(
            self.path,
            "-c",
            "user.name=Release test",
            "-c",
            "user.email=release@example.invalid",
            "-c",
            "tag.gpgsign=false",
            "tag",
            *args,
            tag,
        )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


@pytest.mark.parametrize("annotated", [False, True])
def test_validates_lightweight_and_annotated_main_tags(
    repository: Repository, annotated: bool
) -> None:
    commit = repository.commit("1.0.0rc1")
    repository.update_main()
    repository.tag("v1.0.0-rc.1", annotated=annotated)
    result = release.validate_source(repository.path, "v1.0.0-rc.1")
    assert result.commit == commit
    assert result.version == Version("1.0.0rc1")


def test_rejects_a_tag_off_main(repository: Repository) -> None:
    release.git(repository.path, "switch", "-c", "feature")
    repository.commit("1.0.0")
    repository.tag("v1.0.0")
    with pytest.raises(release.ReleaseError, match="not on origin/main"):
        release.validate_source(repository.path, "v1.0.0")


def test_rejects_a_checkout_other_than_the_tag(repository: Repository) -> None:
    repository.commit("1.0.0")
    repository.tag("v1.0.0")
    repository.commit("1.0.1")
    repository.update_main()
    with pytest.raises(release.ReleaseError, match="checkout"):
        release.validate_source(repository.path, "v1.0.0")


def test_rejects_a_mismatched_project_version(repository: Repository) -> None:
    repository.tag("v1.0.0")
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.validate_source(repository.path, "v1.0.0")


def test_missing_main_ref_fails_closed(repository: Repository) -> None:
    repository.tag("v0.1.0")
    release.git(repository.path, "update-ref", "-d", release.MAIN_REF)
    with pytest.raises(release.ReleaseError, match="could not validate"):
        release.validate_source(repository.path, "v0.1.0")


def test_reads_project_version_from_the_tagged_source(repository: Repository) -> None:
    repository.tag("v0.1.0")
    (repository.path / "pyproject.toml").write_text(
        '[project]\nversion="99.0.0"\n', encoding="utf-8"
    )
    assert release.validate_source(repository.path, "v0.1.0").version == Version("0.1.0")


def test_only_valid_stable_main_tags_participate_in_alias_ordering(repository: Repository) -> None:
    repository.tag("v0.1.0")
    repository.tag("v99.0.0")  # A valid-looking tag with a mismatched project version.
    repository.tag("v0.1")  # A Docker-safe version that would collide with a minor alias.
    repository.commit("2.0.0rc1")
    repository.tag("v2.0.0-rc.1")
    repository.update_main()
    release.git(repository.path, "switch", "-c", "feature")
    repository.commit("100.0.0")
    repository.tag("v100.0.0")
    assert [item.tag for item in release.stable_releases(repository.path)] == ["v0.1.0"]


@pytest.mark.parametrize(
    ("candidate", "others", "expected"),
    [
        ("v1.2.3", (), ("1.2", "latest")),
        ("v1.10.0", ("v1.9.9",), ("1.10", "latest")),
        ("v1.2.3", ("v1.2.4",), ()),
        ("v1.2.3", ("v1.2.3",), ("1.2", "latest")),
        ("v1.2.4", ("v2.0.0", "v1.2.3"), ("1.2",)),
        ("v1.2.3", ("v2.0.0", "v1.2.4"), ()),
        ("v2.0.0", ("v1.99.99",), ("2.0", "latest")),
        ("v1.2.3", ("v2.0.0rc1",), ("1.2", "latest")),
        ("v1.2.3rc1", ("v1.2.2",), ()),
        ("v1.2.3.dev1", (), ()),
        ("v1.2.3.post1", ("v1.2.3",), ("1.2", "latest")),
    ],
)
def test_aliases_cannot_regress_on_reruns_or_out_of_order_releases(
    candidate: str, others: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert (
        release.aliases(
            release.parse_tag(candidate), [release.parse_tag(other) for other in others]
        )
        == expected
    )


def test_an_older_checkout_sees_newer_protected_main_tags(repository: Repository) -> None:
    first = repository.commit("1.2.3")
    repository.tag("v1.2.3")
    repository.commit("1.2.4")
    repository.tag("v1.2.4")
    repository.update_main()
    release.git(repository.path, "checkout", "--detach", first)
    older = release.validate_source(repository.path, "v1.2.3")
    assert release.aliases(older, release.stable_releases(repository.path)) == ()


def test_cli_writes_validated_workflow_outputs(repository: Repository, tmp_path: Path) -> None:
    repository.tag("v0.1.0")
    output = tmp_path / "outputs.txt"
    assert (
        release.main(
            ["promote", "v0.1.0", "--repository", str(repository.path), "--output", str(output)]
        )
        == 0
    )
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["image_tag"] == "0.1.0"
    assert values["prerelease"] == "false"
    assert values["promote_minor"] == "true"
    assert values["promote_latest"] == "true"
    assert values["source_sha"] == release.git(repository.path, "rev-parse", "HEAD")


def test_cli_does_not_write_outputs_for_an_invalid_release(repository: Repository) -> None:
    output = repository.path / "outputs.txt"
    assert (
        release.main(
            ["validate", "vlatest", "--repository", str(repository.path), "--output", str(output)]
        )
        == 1
    )
    assert not output.exists()
