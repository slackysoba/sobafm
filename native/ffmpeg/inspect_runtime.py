"""Inspect the installed native import closure without executing its binaries."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

_GLIBC = {"libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1"}
_WINDOWS = {
    "advapi32.dll",
    "bcrypt.dll",
    "kernel32.dll",
    "msvcrt.dll",
    "ole32.dll",
    "shell32.dll",
    "user32.dll",
    "winmm.dll",
    "ws2_32.dll",
}


def inspect(prefix: Path, target: str, output: Path) -> None:
    windows = target == "windows-amd64"
    binaries = sorted(
        path
        for path in prefix.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            (path.suffix in {".exe", ".dll"})
            if windows
            else (path.parent.name == "bin" or ".so." in path.name)
        )
    )
    if not binaries:
        raise ValueError("native install has no binaries to inspect")
    provided = {
        path.name.casefold() if windows else path.name
        for path in prefix.rglob("*")
        if path.is_file()
    }
    records: list[dict[str, object]] = []
    system: set[str] = set()
    for binary in binaries:
        relative = binary.relative_to(prefix).as_posix()
        undefined: list[str] = []
        if windows:
            text = subprocess.run(  # noqa: S603 - fixed inspector, own fresh install
                ["/usr/bin/x86_64-w64-mingw32-objdump", "-p", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            needed = sorted(set(re.findall(r"DLL Name: ([^\s]+)", text)))
            external = {name.casefold() for name in needed} - provided
            if not external.issubset(_WINDOWS):
                raise ValueError(
                    f"unreviewed Windows runtime imports: {sorted(external - _WINDOWS)}"
                )
            runpath: list[str] = []
            versions: list[str] = []
            characteristic = re.search(r"DllCharacteristics\s+([0-9a-fA-F]+)", text)
            if characteristic is None:
                raise ValueError("PE security characteristics are absent")
            pe_flags = int(characteristic[1], 16)
            sections = subprocess.run(  # noqa: S603 - fixed inspector, own fresh install
                ["/usr/bin/x86_64-w64-mingw32-objdump", "-h", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            relocation = re.search(r"\s\.reloc\s+([0-9a-fA-F]+)\s", sections)
            relocation_bytes = int(relocation[1], 16) if relocation is not None else 0
            if pe_flags & 0x140 != 0x140 or not relocation_bytes:
                raise ValueError("PE runtime lacks ASLR relocation or NX compatibility")
            pe_security: dict[str, int] = {
                "dll_characteristics": pe_flags,
                "relocation_section_bytes": relocation_bytes,
            }
        else:
            pe_security = {}
            text = subprocess.run(  # noqa: S603 - fixed inspector, own fresh install
                ["/usr/bin/readelf", "--dynamic", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            needed = sorted(set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", text)))
            external = set(needed) - provided
            if not external.issubset(_GLIBC):
                raise ValueError(f"unreviewed Linux runtime imports: {sorted(external - _GLIBC)}")
            runpath = re.findall(r"\((?:RUNPATH|RPATH)\).*\[([^]]+)\]", text)
            expected = "$ORIGIN/../lib" if binary.parent.name == "bin" else "$ORIGIN"
            if runpath != [expected]:
                raise ValueError("native install does not preserve relative shared-library loading")
            version_text = subprocess.run(  # noqa: S603 - fixed inspector, own fresh install
                ["/usr/bin/readelf", "--version-info", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            versions = sorted(set(re.findall(r"\bGLIBC_[0-9.]+\b", version_text)))
            symbol_text = subprocess.run(  # noqa: S603 - fixed inspector, own fresh install
                ["/usr/bin/readelf", "--wide", "--dyn-syms", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            undefined = sorted(
                {
                    fields[7]
                    for line in symbol_text.splitlines()
                    if len(fields := line.split()) >= 8 and fields[6] == "UND"
                }
            )
            if {name.split("@")[0] for name in undefined} & {
                "iconv",
                "iconv_open",
                "iconv_close",
                "libiconv",
                "libiconv_open",
                "libiconv_close",
            }:
                raise ValueError("minimal audio runtime unexpectedly imports iconv")
        system.update(external)
        with binary.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append(
            {
                "path": relative,
                "sha256": sha,
                "size": binary.stat().st_size,
                "needed": needed,
                "runpath": runpath,
                "glibc_symbol_versions": versions,
                "pe_security": pe_security,
                "undefined_symbols": undefined,
            }
        )
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            {
                "schema_version": 1,
                "target": target,
                "binaries": records,
                "system_libraries": sorted(system),
                "system_distribution": "provided by the supported OS; not in this runtime archive",
            },
            stream,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: inspect_runtime.py PREFIX TARGET OUTPUT")
    inspect(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
