# Installing XynClaim

Windows 10 or 11, 64-bit. No Python needed.

## Install

Run `XynClaim_<version>_x64-setup.exe`. It installs for your Windows account only (no admin prompt) to `%LOCALAPPDATA%\Programs\XynClaim` and adds Desktop and Start Menu shortcuts. Running a newer installer over an old one updates it in place and keeps your settings.

The installer isn't code-signed yet, so SmartScreen may show "Windows protected your PC". Click **More info**, then **Run anyway**.

## Run

Start **XynClaim** from a shortcut. The first run needs a visible Roblox client and a desktop large enough to show an exact 1920 × 1080 Roblox client area. XynClaim accounts for the title bar and borders itself; don't manually switch your monitor to 1080p.

Before starting, set the account's available slot count (1–12), choose the slots, and choose My server or Shared code. Public fallback stays off unless you opt into it for that run. Read [README.md](README.md) before enabling a schedule or Discord notification.

## Uninstall

Settings > Apps > Installed apps > **XynClaim** > Uninstall. This removes the app, its shortcuts and the `XynClaim Daily` scheduled task. Reports, planner settings, notification settings and claim state in `%LOCALAPPDATA%\XynClaim` stay; delete that folder too if you want them gone.

## Building from source

From the checkout root, install the dependencies and start the desktop app:

```powershell
python -m pip install -r requirements.txt
python -m xynclaim
```

The Python code lives in `xynclaim/`; `ui/` and `assets/reference/` stay at the
checkout root. The version is defined in `xynclaim/__init__.py`.

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Needs 64-bit Python 3.11+. It builds in a private `.build-venv`, then writes `dist\XynClaim\` and, if NSIS is available, `dist\XynClaim_<version>_x64-setup.exe`. Tests:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
node --test ui/ui-contract.test.mjs
```

## Troubleshooting

- **The window doesn't open** — install or repair the Microsoft Edge WebView2 Runtime (it ships with Windows 11 and current Windows 10).
- **Reward reading says OCR is unavailable** — add an English language pack in Windows Settings > Time & language. The Setup check on the Settings screen shows this.
- **Roblox can't be controlled** — leave Roblox visible, focused and unoccluded. Let XynClaim set the controlled window size; it restores the prior size and state after the run.
- **The scheduled run didn't happen** — the task only runs while you're signed in. Check the Schedule screen for the next run time, or open Task Scheduler and look for `XynClaim Daily`.
- **Shared code is rejected** — ask the owner for the current universal code. It may have been regenerated, the owner may no longer own the private network, or DBOG may have rejected the join. Don't enable public fallback unless joining a public server is acceptable.
