param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][string]$Python
)

$ErrorActionPreference = 'Stop'
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$outputPath = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) {
    throw 'Output must be new. Existing and partial outputs are preserved.'
}
New-Item -ItemType Directory -Path $outputPath | Out-Null
$previousCache = $env:UV_CACHE_DIR
try {
    $env:UV_CACHE_DIR = Join-Path $outputPath 'uv-cache'
    $buildEnv = Join-Path $outputPath 'build-env'
    & $pythonPath -m venv --without-pip $buildEnv
    if ($LASTEXITCODE -ne 0) { throw 'Build environment creation failed.' }
    $buildPython = Join-Path $buildEnv 'Scripts/python.exe'
    uv pip sync --python $buildPython --require-hashes --only-binary :all: `
        (Join-Path $PSScriptRoot 'build-requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Pinned build dependency installation failed.' }
    & $buildPython -I -B (Join-Path $PSScriptRoot 'prepare_retirement.py') `
        --archive $archivePath --output (Join-Path $outputPath 'candidate') `
        --extension (Join-Path $PSScriptRoot 'CONNECTION-RETIREMENT.toml')
    if ($LASTEXITCODE -ne 0) { throw 'Candidate build failed; partial outputs retained.' }
}
finally {
    $env:UV_CACHE_DIR = $previousCache
}
