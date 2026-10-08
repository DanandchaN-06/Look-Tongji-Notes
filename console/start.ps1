# Portable launcher; no configured user path or Agent-specific interpreter.
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$consoleRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $consoleRoot
Set-Location -LiteralPath $projectRoot
$consoleScript = Join-Path $consoleRoot 'start.py'
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($env:LOOK_TONGJI_PYTHON) {
    & $env:LOOK_TONGJI_PYTHON $consoleScript @args
} elseif (Test-Path -LiteralPath $projectPython) {
    & $projectPython $consoleScript @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $consoleScript @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $consoleScript @args
} else {
    throw 'Python 3 was not found. Install Python and follow console/README.md.'
}
exit $LASTEXITCODE
