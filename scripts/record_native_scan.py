"""Record native SBOM scanner matches and omissions without granting readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Package(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    name: str
    version: str
    ecosystem: str


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str


class PackageResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    package: Package
    vulnerabilities: list[Finding] = Field(default_factory=list[Finding])


class Result(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    packages: list[PackageResult]


class Scan(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    results: list[Result]


def record_scan(sbom: Path, report: Path, exit_code: int, output: Path) -> None:
    """Exit 1 denotes retained findings; scanner execution/parse errors fail the job."""
    if exit_code not in {0, 1}:
        raise ValueError("native scanner failed; its report cannot establish coverage")
    scan = Scan.model_validate_json(report.read_bytes())
    document = json.loads(sbom.read_bytes())
    packages = [package for result in scan.results for package in result.packages]
    if not any(item.package.ecosystem.startswith("Debian:") for item in packages):
        raise ValueError("native scanner reported no supported Debian component packages")
    coverage = {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "scanner": "OSV-Scanner 2.6.0; osv-scalibr 0.5.2",
        "scanner_sha256": "ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108",
        "invocation": "scan source --format json --all-packages --all-vulns --lockfile SBOM",
        "database": "online OSV API at query time; no fixed database snapshot timestamp exposed",
        "scanner_exit_code": exit_code,
        "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "reported_packages": [
            {
                **item.package.model_dump(),
                "advisories": sorted({v.id for v in item.vulnerabilities}),
            }
            for item in packages
        ],
        "inventoried_components": [
            {
                "name": package["name"],
                "version": package.get("versionInfo"),
                "scope": package.get("comment"),
                "references": package.get("externalRefs", []),
            }
            for package in document["packages"]
            if package["SPDXID"].startswith("SPDXRef-Component-")
        ],
        "qualification": "coverage record only; findings are not suppressed or approved",
        "gaps": [
            "Whole-source-package matches do not establish incorporation or reachability.",
            "Custom FFmpeg, copied members and OS implementations need primary-source review.",
            "No match, unsupported identifier or zero findings is not proof of no vulnerabilities.",
            "Require selected-code/platform dispositions and independent review before approval.",
        ],
    }
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(coverage, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("exit_code", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    record_scan(args.sbom, args.report, args.exit_code, args.output)


if __name__ == "__main__":
    main()
