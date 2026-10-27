"""Verify that the installed src-layout package is usable away from the checkout."""

import subprocess
import sys
from pathlib import Path


def test_installed_package_imports(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sobafm; from importlib.metadata import distribution; "
            "assert distribution('sobafm').metadata['Name'] == 'sobafm'",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
