# Activate ESP-IDF 5.5.3 and run one command inside the activated environment.
#
# Why this exists instead of a shell one-liner:
#   - ESP-IDF refuses to activate under Git Bash. `idf_tools.py` aborts with
#     "MSys/Mingw is not supported" whenever MSYSTEM is set, and MSYSTEM=MINGW64
#     is always set in this project's shell. Unsetting it is not enough because
#     the activation script still resolves the wrong interpreter.
#   - The installer pinned its virtualenv to Python 3.11, but the first `python`
#     on PATH is 3.10.11, so export.sh/export.ps1 look for idf5.5_py3.10_env,
#     which does not exist. Prepending the 3.11 venv fixes that.
#
# Usage:
#   powershell -NoProfile -File tools/idf-run.ps1 -Command 'idf.py --version'
#   powershell -NoProfile -File tools/idf-run.ps1 -Command 'idf.py build' -WorkDir .
#
# `-Command` runs with the working directory at the repository root by default.
param(
    [Parameter(Mandatory = $true)][string]$Command,
    [string]$WorkDir = ''
)

$ErrorActionPreference = 'Stop'

# Git Bash leaks these into any child process; ESP-IDF treats their presence as
# an unsupported host, so they are removed before anything else runs.
foreach ($v in 'MSYSTEM', 'MINGW_PREFIX', 'MSYS', 'OSTYPE') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -Force }
}

$idfRoot = 'D:\Espressif'
$idfPath = Join-Path $idfRoot 'frameworks\esp-idf-v5.5.3'
$pyVenv  = Join-Path $idfRoot 'python_env\idf5.5_py3.11_env\Scripts'

if (-not (Test-Path $idfPath)) { throw "ESP-IDF not found at $idfPath" }
if (-not (Test-Path $pyVenv))  { throw "IDF python venv not found at $pyVenv" }

# The venv must come first or activation resolves the system Python 3.10 and
# fails looking for a py3.10 environment the installer never created.
$env:PATH = "$pyVenv;$env:PATH"

# export.ps1 writes its progress text through the error stream, and PowerShell
# renders anything on that stream as a red NativeCommandError block even when
# the script succeeded. ErrorActionPreference is relaxed for the call and the
# streams are swallowed, so only the requested command's own output reaches the
# caller. IDF_PATH is then checked directly: the environment variable is the
# real evidence that activation worked, not the absence of red text.
$activate = Join-Path $idfPath 'export.ps1'
$previousEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $activate *> $null
$ErrorActionPreference = $previousEap

if (-not $env:IDF_PATH) { throw 'ESP-IDF activation produced no IDF_PATH' }

$repoRoot = Split-Path -Parent $PSScriptRoot
$runDir = if ($WorkDir) { $WorkDir } else { $repoRoot }
Push-Location $runDir
try {
    Invoke-Expression $Command
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
