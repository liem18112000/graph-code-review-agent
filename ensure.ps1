# Bootstrap for Windows PowerShell.
#
# ensure.py cannot check whether Python exists -- it needs Python to run. This
# shim is the only part that must work with nothing installed, so it does exactly
# one thing: find a Python >= 3.11, then hand off.
#
#   powershell -ExecutionPolicy Bypass -File ensure.ps1
#   powershell -ExecutionPolicy Bypass -File ensure.ps1 --deep

$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Py($exe) {
    try {
        & $exe -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$py = $null
# `py -3` is the Windows launcher and usually resolves the newest install
foreach ($c in @('python3.14','python3.13','python3.12','python3.11','python3','python')) {
    if ((Get-Command $c -ErrorAction SilentlyContinue) -and (Test-Py $c)) { $py = $c; break }
}
if (-not $py -and (Get-Command 'py' -ErrorAction SilentlyContinue)) {
    if (Test-Py 'py') { $py = 'py' }
}

if (-not $py) {
    Write-Host "MUST: Python >= 3.11 not found."
    Write-Host ""
    $old = Get-Command python -ErrorAction SilentlyContinue
    if ($old) {
        Write-Host "  (found $($old.Source), but it is older than 3.11 -- tomllib is stdlib from 3.11)"
    }
    Write-Host "  winget install Python.Python.3.12"
    Write-Host "  # or: https://www.python.org/downloads/  (tick 'Add python.exe to PATH')"
    Write-Host ""
    Write-Host "Then reopen the terminal and re-run: powershell -File ensure.ps1"
    exit 1
}

& $py (Join-Path $dir 'ensure.py') @args
exit $LASTEXITCODE
