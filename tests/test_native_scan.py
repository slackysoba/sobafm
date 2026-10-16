"""Scanner coverage retains matches and refuses failed or empty scans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.record_native_scan import record_scan


@pytest.mark.parametrize("exit_code", [0, 1, 128])
def test_coverage_is_a_record_and_never_a_runtime_approval(tmp_path: Path, exit_code: int) -> None:
    sbom, report, output = (
        tmp_path / name for name in ("sbom.json", "report.json", "coverage.json")
    )
    sbom.write_text(json.dumps({"packages": [{"SPDXID": "SPDXRef-Component-0", "name": "ffmpeg"}]}))
    report.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "packages": [
                            {
                                "package": {
                                    "name": "glibc",
                                    "version": "1",
                                    "ecosystem": "Debian:13",
                                },
                                "vulnerabilities": [{"id": "CVE-fixture"}],
                            }
                        ]
                    }
                ]
            }
        )
    )
    if exit_code == 128:
        with pytest.raises(ValueError, match="scanner failed"):
            record_scan(sbom, report, exit_code, output)
        assert not output.exists()
    else:
        record_scan(sbom, report, exit_code, output)
        coverage = json.loads(output.read_bytes())
        assert coverage["reported_packages"][0]["advisories"] == ["CVE-fixture"]
        assert coverage["scanner_exit_code"] == exit_code
        assert (
            coverage["qualification"]
            == "coverage record only; findings are not suppressed or approved"
        )


def test_no_match_cannot_pass_the_coverage_job(tmp_path: Path) -> None:
    sbom, report, output = (
        tmp_path / name for name in ("sbom.json", "report.json", "coverage.json")
    )
    sbom.write_text('{"packages":[]}')
    report.write_text('{"results":[]}')
    with pytest.raises(ValueError, match="reported no supported"):
        record_scan(sbom, report, 0, output)
    assert not output.exists()
