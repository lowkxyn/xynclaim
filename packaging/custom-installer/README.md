# Custom installer

The native Rust host embeds one HTML document and the existing NSIS installer.
Tao owns the 720 x 460 frameless window; Wry uses the installed WebView2 runtime.
The window, typography, sheen, screen transitions and progress glow are plain
HTML and CSS. `app.json` holds the application name, registry keys,
executable name, relative artwork paths and application-specific copy. The HTML
has placeholders rather than application names. The NSIS backend and release
build entry point remain specific to this application.

On Install, the host validates the destination, extracts the embedded backend to
a unique temporary directory, then starts it without a shell or elevation:

```text
xyn-payload.exe /S [/NoDesktop] [/NoStartMenu] /ProgressFile=<private-status-file> /D=<folder with spaces>
```

NSIS requires `/D=` last and unquoted; Rust uses Windows `raw_arg` for that part.
Shortcut flags skip creating the selected shortcut; they don't delete a shortcut
from an earlier installation. Their defaults remain enabled in plain NSIS builds.
The optional progress file signals when NSIS has removed the previous `_internal`
directory and started copying. The host sums the expected payload file lengths
in the destination against their total uncompressed size. This is an estimate of
copy progress, sampled every 200 ms, capped at 98% until the process succeeds.
Unchanged top-level files in an upgrade can contribute their existing size.
Success also requires all expected file lengths, the uninstaller and registry
registration to exist. This isn't a content hash verification or rollback system.

NSIS remains responsible for per-user installation, shortcuts, registration,
the running-window guard and uninstallation. No uninstaller is invoked by the
host. An upgrade runs in place. There is no "uninstall first" step, because
uninstalling removes the daily task.
Partial installation failures can leave files behind; the UI reports the backend
exit code and allows retry. Close and Escape are blocked while installation runs.
Launching the installed app is offered only after verified success and an
explicit click. If WebView2 creation fails, the host waits for the same embedded
backend with its normal UI and default options instead.

## Build

Prerequisites: Windows x64, Rust/MSVC and Windows SDK, NSIS, Python, and an existing
frozen application in `dist/XynClaim`. Cargo.lock is retained. Build from repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build-custom.ps1
```

This reads the frozen payload and writes only `work/custom-installer` by default:

- `XynClaim_<version>_x64-setup.exe`, the single executable to distribute.
- `CUSTOM-INSTALLER-NOTICES.txt`, also installed beside the application's notices.
- `bundle/`, the embedded HTML, backend and expected payload manifest.
- `target/`, Cargo outputs.

The full application build has an **opt-in** `-CustomInstaller` step:

```powershell
# These commands rebuild everything in dist.
powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1 -CustomInstaller
powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1 -PlainInstaller
```

Omitting both switches preserves the original plain NSIS build. `-SkipInstaller`
still skips either installer. Don't combine `-CustomInstaller` and `-PlainInstaller`.
`build-custom.ps1` also accepts `-Source`, `-Version` and `-OutputDirectory`.
Build scripts compile installers; they never run an installer or the application.

## Safe preview and checks

```powershell
.\work\custom-installer\XynClaim_2.0.0_x64-setup.exe --dry-run --dry-run-fresh
.\work\custom-installer\XynClaim_2.0.0_x64-setup.exe --dry-run
.\work\custom-installer\XynClaim_2.0.0_x64-setup.exe --dry-run --dry-run-error
.\work\custom-installer\XynClaim_2.0.0_x64-setup.exe --dry-run --test-no-webview
$env:XYN_INSTALLER_BUNDLE = (Resolve-Path .\work\custom-installer\bundle).Path
$env:CARGO_TARGET_DIR = Join-Path $PWD 'work\custom-installer\target'
cargo test --locked --release --manifest-path .\packaging\custom-installer\Cargo.toml
cargo fmt --check --manifest-path .\packaging\custom-installer\Cargo.toml
```

Dry-run lives in the Rust execution boundary. It never extracts or starts NSIS,
creates install files or shortcuts, writes registration, runs an uninstaller,
changes the daily task, or launches the application, including from the finish
button. It reads installation registration to select the update screen and
validates folder metadata. `--dry-run-fresh` forces the fresh welcome screen.
`--dry-run-error` simulates a backend failure. `--test-no-webview` exercises the
dry-run fallback guard; it is rejected unless `--dry-run` is also present.
WebView2 uses its own uniquely created temporary profile, not a browser profile.

Real install testing changes the application's HKCU registration even with a
temporary destination, so back up the registry keys and shortcuts first. Don't use
the uninstaller to clean up a test install: it deletes the daily scheduled task.

## Third-party licences

`prepare.py` uses the locked Cargo graph filtered for Windows x64, retains the
licence texts of its dependencies (including build dependencies), and appends
the fonts' notices and OFL. Missing texts fail the build. `webview2-rs-MIT.txt`
fills omissions in three published crates; its upstream copies were verified
at commits `edc2caf886175ccaebe86078c9cfe1ae2a187328` and
`dffa41a8a46d3f5565eefbff2de57d38d399f158` from
https://github.com/wravery/webview2-rs. Existing application notices stay separate.
The WebView2 runtime itself isn't redistributed.

## Release considerations

The host has an embedded XC icon, x64 PE metadata and an `asInvoker` manifest.
The build helper remaps source and dependency paths in Rust diagnostic strings
so the builder's home directory isn't embedded in the native host.
It is unsigned unless the owner separately signs it. No UPX/packer is used.
An unsigned executable that extracts and starts a second installer may attract
SmartScreen reputation prompts or antivirus heuristic detections. Sign both
backend and outer host when a signing identity is available, and test the final
signed artifact on a clean Windows machine. No detection-free claim is made.
