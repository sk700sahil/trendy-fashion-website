# Run from the repository root, or from another directory using this script's path.
# A project-local uv install is supported; pywrangler also needs uv on PATH.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$localUv = Join-Path $projectRoot '.tools\bin\uv.exe'
$previousPath = $env:PATH
if (Test-Path -LiteralPath $localUv) {
    $env:PATH = (Split-Path -Parent $localUv) + [IO.Path]::PathSeparator + $env:PATH
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: python -m pip install uv'
}
Push-Location $projectRoot
try {
    & uv run pywrangler @args
    $workerExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PATH = $previousPath
}
exit $workerExitCode
