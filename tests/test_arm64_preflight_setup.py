"""Exercise setup admission with shell fakes; never download or dispatch anything."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "evaluations/linux_resource/arm64_preflight_setup.sh"


def run_setup(
    directory: Path, phase: str, overrides: str = ":", *, free: int = 30_000_000_000
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; available_bytes() { printf "%s" "$TEST_FREE"; }; '
            + overrides
            + '; main "$2"',
            "setup-test",
            str(SCRIPT),
            phase,
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "PREFLIGHT_ROOT": str(directory / "sobafm-arm64-preflight-123-1"),
            "PREFLIGHT_WORKSPACE": str(directory),
            "PREFLIGHT_TOOL_CACHE": str(directory),
            "SETUP_UV_PATH": str(directory / "uv/0.13.6/aarch64/uv"),
            "TEST_FREE": str(free),
            "UNRELATED_SENTINEL": "must-not-reach-installer",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def fake_installer(directory: Path, body: str) -> Path:
    root = directory / "sobafm-arm64-preflight-123-1"
    root.mkdir()
    executable = directory / "uv/0.13.6/aarch64/uv"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    executable.chmod(0o700)
    return root


def test_admission_reserves_setup_headroom_before_creating_files(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "prepare", free=25_536_870_911)
    assert result.returncode == 1
    assert "free_space_below_reservation" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_unobservable_storage_stops_before_setup(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "prepare", "available_bytes() { return 1; }")
    assert result.returncode == 1
    assert "storage_unobservable" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_existing_task_directory_is_never_reused_or_removed(tmp_path: Path) -> None:
    root = tmp_path / "sobafm-arm64-preflight-123-1"
    root.mkdir()
    sentinel = root / "retained"
    sentinel.write_text("unfinished", encoding="utf-8")
    result = run_setup(tmp_path, "prepare")
    assert result.returncode == 1
    assert "setup_directory_already_exists" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "unfinished"


def test_prepare_and_real_usage_measurement(tmp_path: Path) -> None:
    result = run_setup(tmp_path, "prepare", free=25_536_870_912)
    assert result.returncode == 0, result.stderr
    assert '"setup_bytes":' in result.stdout
    assert "storage_checkpoint_passed" in result.stdout
    assert (tmp_path / "sobafm-arm64-preflight-123-1/tmp").is_dir()


def test_oversize_setup_cannot_start_installer(tmp_path: Path) -> None:
    root = fake_installer(tmp_path, 'touch "$UV_PYTHON_INSTALL_DIR/started"\n')
    result = run_setup(tmp_path, "install", "setup_bytes() { printf 536870913; }")
    assert result.returncode == 1
    assert '"setup_bytes":536870913' in result.stdout
    assert "setup_storage_limit_exceeded" in result.stdout
    assert not (root / "python/started").exists()


def test_installer_receives_only_exact_request_and_isolated_paths(tmp_path: Path) -> None:
    root = fake_installer(
        tmp_path,
        'test "$*" = "--no-config --quiet python install --no-bin '
        'cpython-3.14.10-linux-aarch64-gnu"\n'
        'test "${UNRELATED_SENTINEL-unset}" = unset\n'
        'test "$UV_HTTP_RETRIES" = 0\n'
        'test "$HOME" = /nonexistent\n'
        'test "$UV_CACHE_DIR" = "${UV_PYTHON_INSTALL_DIR%/python}/cache"\n'
        'mkdir -p "$UV_PYTHON_INSTALL_DIR/cpython-3.14.10-linux-aarch64-gnu/bin"\n'
        'target="$UV_PYTHON_INSTALL_DIR/cpython-3.14.10-linux-aarch64-gnu/bin/python3.14"\n'
        'printf "fake" > "$target"\nchmod 700 "$target"\n'
        'printf "suppressed diagnostic" >&2\n',
    )
    result = run_setup(tmp_path, "install")
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stderr == ""
    assert "suppressed diagnostic" not in result.stdout
    assert (root / "python/cpython-3.14.10-linux-aarch64-gnu/bin/python3.14").exists()


def test_failed_install_is_not_retried_and_does_not_claim_release(tmp_path: Path) -> None:
    root = fake_installer(tmp_path, 'printf x >> "$UV_CACHE_DIR/attempts"\nexit 23\n')
    (root / "cache").mkdir()
    result = run_setup(tmp_path, "install")
    assert result.returncode == 1
    assert '"installer_exit":23' in result.stdout
    assert "python_314_setup_failed" in result.stdout
    assert "not_qualified_by_setup" in result.stdout
    assert (root / "cache/attempts").read_text(encoding="utf-8") == "x"


def test_post_install_growth_stops_observer_admission(tmp_path: Path) -> None:
    root = fake_installer(tmp_path, 'touch "$UV_CACHE_DIR/grew"\n')
    (root / "cache").mkdir()
    result = run_setup(
        tmp_path,
        "install",
        'setup_bytes() { if [[ -e "$PREFLIGHT_ROOT/cache/grew" ]]; then '
        "printf 536870913; else printf 4096; fi; }",
    )
    assert result.returncode == 1
    assert "setup_storage_limit_exceeded" in result.stdout


def test_missing_installer_stays_unqualified(tmp_path: Path) -> None:
    (tmp_path / "sobafm-arm64-preflight-123-1").mkdir()
    result = run_setup(tmp_path, "install")
    assert result.returncode == 1
    assert "pinned_uv_unavailable" in result.stdout


def test_existing_uv_cache_stops_before_any_change(tmp_path: Path) -> None:
    cache = tmp_path / "uv/0.13.6"
    cache.mkdir(parents=True)
    sentinel = cache / "preexisting"
    sentinel.write_text("preserve", encoding="utf-8")
    result = run_setup(tmp_path, "prepare")
    assert result.returncode == 1
    assert "uv_tool_cache_already_exists" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "sobafm-arm64-preflight-123-1").exists()


def test_uv_cache_and_unknown_temp_reservation_are_charged(tmp_path: Path) -> None:
    fake_installer(tmp_path, "exit 0\n")
    result = run_setup(
        tmp_path,
        "check",
        'setup_bytes() { if [[ "$1" == */uv/0.13.6 ]]; then printf 402653184; else printf 1; fi; }',
    )
    assert result.returncode == 1
    assert '"uv_tool_cache_bytes":402653184' in result.stdout
    assert '"accounted_bytes":536870913' in result.stdout
    assert '"bootstrap_temp_usage":"unmeasured"' in result.stdout
    assert "setup_storage_limit_exceeded" in result.stdout


def test_unexpected_action_path_cannot_run_installer(tmp_path: Path) -> None:
    fake_installer(tmp_path, "printf must_not_run\n")
    result = run_setup(tmp_path, "install", "SETUP_UV_PATH=/unexpected/uv")
    assert result.returncode == 1
    assert "unexpected_uv_path" in result.stdout
    assert "must_not_run" not in result.stdout


def test_workflow_does_not_observe_after_a_failed_setup(tmp_path: Path) -> None:
    root = tmp_path / "sobafm-arm64-preflight-123-1"
    python = root / "python/cpython-3.14.10-linux-aarch64-gnu/bin/python3.14"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nprintf observer_must_not_run\n", encoding="utf-8")
    python.chmod(0o700)
    repository = SCRIPT.parents[2]
    workflow = (repository / ".github/workflows/arm64-runner-preflight.yaml").read_text(
        encoding="utf-8"
    )
    body = workflow.split("        run: |\n", maxsplit=1)[1]
    body = "\n".join(line.removeprefix("          ") for line in body.splitlines())
    body = body.replace("${{ github.sha }}", "1" * 40)
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", body],
        cwd=repository,
        env={
            "PATH": "/usr/bin:/bin",
            "PREFLIGHT_ROOT": str(root),
            "PREFLIGHT_WORKSPACE": str(repository),
            "PREFLIGHT_TOOL_CACHE": str(tmp_path),
            "ADMISSION_OUTCOME": "success",
            "UV_OUTCOME": "failure",
            "PYTHON_OUTCOME": "skipped",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 1
    assert "observer_must_not_run" not in result.stdout
    assert "python_314_setup_unavailable" in result.stdout
    assert '"native_runtime_qualification":"not_run"' in result.stdout
    assert "not_qualified_by_fallback" in result.stdout
