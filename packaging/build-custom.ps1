<# Build only the installer, using an existing frozen payload. Never runs the app or installer.
   Default output: work\custom-installer. Use build.ps1 -CustomInstaller for a full release build.
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$Version,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$HostRoot = Join-Path $PSScriptRoot 'custom-installer'
if (-not $Source) { $Source = Join-Path $Root 'dist\XynClaim' }
if (-not $Version) {
    $Version = (Select-String -Path (Join-Path $Root 'xynclaim\__init__.py') -Pattern '^APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must have three numeric components.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $Root 'work\custom-installer' }
$Source = (Resolve-Path -LiteralPath $Source).Path
if (-not (Test-Path -LiteralPath (Join-Path $Source 'XynClaim.exe'))) { throw 'Frozen payload missing.' }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$Bundle = Join-Path $Root 'work\custom-installer\bundle'
New-Item -ItemType Directory -Force -Path $Bundle | Out-Null
$Py = Join-Path $Root '.build-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Py)) { $Py = (Get-Command python -ErrorAction Stop).Source }
$MakeNsis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $MakeNsis) { $MakeNsis = Join-Path $env:LOCALAPPDATA 'tauri\NSIS\makensis.exe' }
if (-not (Test-Path -LiteralPath $MakeNsis)) { throw 'makensis not found.' }
$PreviousBundle = $env:XYN_INSTALLER_BUNDLE
$PreviousTarget = $env:CARGO_TARGET_DIR
$PreviousRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
try {
    $env:XYN_INSTALLER_BUNDLE = $Bundle
    $env:CARGO_TARGET_DIR = Join-Path $Root 'work\custom-installer\target'
    # Cargo's encoded form preserves paths with spaces and hides build-machine paths in panic strings.
    $RustFlags = @(
        if ($PreviousRustFlags) { $PreviousRustFlags -split [char]31 }
        elseif ($env:RUSTFLAGS) { $env:RUSTFLAGS -split '\s+' | Where-Object { $_ } }
    )
    $RustFlags += "--remap-path-prefix=$env:USERPROFILE=/.build-user"
    $RustFlags += "--remap-path-prefix=$Root=."
    $env:CARGO_ENCODED_RUSTFLAGS = $RustFlags -join [char]31
    & cargo fetch --locked --manifest-path (Join-Path $HostRoot 'Cargo.toml')
    if ($LASTEXITCODE -ne 0) { throw 'cargo fetch failed' }
    & $Py (Join-Path $HostRoot 'prepare.py') $Bundle --source $Source --version $Version
    if ($LASTEXITCODE -ne 0) { throw 'Resource and licence generation failed' }
    & $MakeNsis /V2 "/DVERSION=$Version" "/DSRC=$Source" "/DOUTFILE=$(Join-Path $Bundle 'xyn-payload.exe')" "/DCUSTOM_NOTICES=$(Join-Path $Bundle 'CUSTOM-INSTALLER-NOTICES.txt')" (Join-Path $PSScriptRoot 'installer.nsi')
    if ($LASTEXITCODE -ne 0) { throw 'makensis failed' }
    & cargo build --locked --release --manifest-path (Join-Path $HostRoot 'Cargo.toml')
    if ($LASTEXITCODE -ne 0) { throw 'Custom installer compilation failed' }
    $Output = Join-Path $OutputDirectory "XynClaim_${Version}_x64-setup.exe"
    Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR 'release\xyn-installer.exe') -Destination $Output
    Copy-Item -LiteralPath (Join-Path $Bundle 'CUSTOM-INSTALLER-NOTICES.txt') -Destination $OutputDirectory
    Write-Host "Installer: $Output ($((Get-Item -LiteralPath $Output).Length) bytes)"
} finally {
    $env:XYN_INSTALLER_BUNDLE = $PreviousBundle
    $env:CARGO_TARGET_DIR = $PreviousTarget
    $env:CARGO_ENCODED_RUSTFLAGS = $PreviousRustFlags
}
