<#
    Builds dist\XynClaim (PyInstaller onedir) and dist\XynClaim_<version>_x64-setup.exe (NSIS).

    Usage (from anywhere):
        powershell -ExecutionPolicy Bypass -File <path to repo>\packaging\build.ps1

    Uses a private build venv (.build-venv) so the global Python stays untouched.
    NSIS: uses makensis on PATH, else the copy Tauri keeps in %LOCALAPPDATA%\tauri\NSIS.
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$CustomInstaller,
    [switch]$PlainInstaller
)

$ErrorActionPreference = 'Stop'
if ($CustomInstaller -and $PlainInstaller) { throw 'Choose -CustomInstaller or -PlainInstaller.' }
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.build-venv'
$Py = Join-Path $Venv 'Scripts\python.exe'

$Version = (Select-String -Path (Join-Path $Root 'xynclaim\__init__.py') -Pattern '^APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
if (-not $Version) { throw 'APP_VERSION not found in xynclaim\__init__.py' }
Write-Host "XynClaim $Version"

if (-not (Test-Path $Py)) {
    Write-Host 'Creating build venv'
    & py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { & python -m venv $Venv }
}
& $Py -m pip install --quiet --disable-pip-version-check -r (Join-Path $Root 'requirements.txt') 'pyinstaller==6.*'
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

Push-Location $Root
try {
    & $Py -m PyInstaller --noconfirm --clean --distpath (Join-Path $Root 'dist') --workpath (Join-Path $Root 'build') (Join-Path $PSScriptRoot 'XynClaim.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
} finally {
    Pop-Location
}

$Exe = Join-Path $Root 'dist\XynClaim\XynClaim.exe'
if (-not (Test-Path $Exe)) { throw "Build output missing: $Exe" }
& $Py (Join-Path $PSScriptRoot 'notices.py') (Join-Path $Root 'dist\XynClaim\THIRD-PARTY-NOTICES.txt')
if ($LASTEXITCODE -ne 0) { throw 'Writing THIRD-PARTY-NOTICES.txt failed' }
Write-Host "Built $Exe"
if ($SkipInstaller) { return }
if ($CustomInstaller) {
    & (Join-Path $PSScriptRoot 'build-custom.ps1') -Source (Join-Path $Root 'dist\XynClaim') -Version $Version -OutputDirectory (Join-Path $Root 'dist')
    return
}

$MakeNsis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $MakeNsis) { $MakeNsis = Join-Path $env:LOCALAPPDATA 'tauri\NSIS\makensis.exe' }
if (-not (Test-Path $MakeNsis)) { throw 'makensis not found. Install NSIS or build XynMacro once so Tauri fetches it.' }

& $MakeNsis /V2 "/DVERSION=$Version" "/DSRC=$(Join-Path $Root 'dist\XynClaim')" (Join-Path $PSScriptRoot 'installer.nsi')
if ($LASTEXITCODE -ne 0) { throw 'makensis failed' }
Write-Host "Installer: $(Join-Path $Root "dist\XynClaim_${Version}_x64-setup.exe")"
