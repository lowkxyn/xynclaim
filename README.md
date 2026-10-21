# XynClaim

XynClaim is a Windows desktop helper for claiming Dragon Ball Online Generations (DBOG) daily-login rewards across selected character slots. It uses visible screen matching and normal mouse and keyboard input only. It does not inject into Roblox or read Roblox memory.

**[Download the latest release](https://github.com/lowkxyn/xynclaim/releases/latest)**

- Claims every selected character slot in one run, or on a daily schedule.
- Streak planner: claim a slot every day, keep slots aligned on the same reward day, or hold them.
- History by day, per-slot stats and receipts, all stored on your PC.
- Never buys anything and never clicks Restore Streak or any Robux button.
- Optional Discord notification when a run finishes.

The interface is a small local app hosted by Microsoft Edge WebView2, with a compact always-on-top pill while a run is going. It doesn't bundle Chromium or Electron.

## Requirements

Windows 10 or 11 (64-bit), a display that fits a 1920 × 1080 Roblox window, and Microsoft Edge WebView2 (already on most PCs). To build from source, see [INSTALL.md](INSTALL.md).

## Install and start

1. Download `XynClaim_<version>_x64-setup.exe` from the [latest release](https://github.com/lowkxyn/xynclaim/releases/latest) and run it. No Python needed, no admin prompt.
2. Windows SmartScreen may warn on first run because the installer isn't signed. Click **More info**, then **Run anyway**.
3. Start **XynClaim** from the Desktop or Start Menu shortcut.

It installs for your Windows account only, to `%LOCALAPPDATA%\Programs\XynClaim`. It doesn't install a service, driver or extension. See [INSTALL.md](INSTALL.md) for removal and troubleshooting.

Roblox may already be open, or XynClaim can launch Dragon Generations. If it is already open, begin from the DBOG main menu.

## Quick start

1. In **Settings**, set how many character slots your account has.
2. Pick your server access: **My server** if you own a private server, or **Shared code** for a friend's.
3. Press **Start run** (or `F6`) and leave the PC alone until the pill says it's done. Clicking another window stops the run.
4. Optional: turn on the daily **Schedule** so it claims by itself after each reset.

## Before a run

XynClaim temporarily controls Roblox in an exact **1920 × 1080 client area**. This is the inside of the Roblox window, not its outer size: XynClaim measures the title bar and borders, chooses an outer window size that produces that client area, keeps it within the usable desktop, then restores the previous window bounds and fullscreen state when the run ends. It does not change your monitor resolution.

Keep the desktop unlocked and Roblox visible. Choose **Current**, **Primary**, or **Second display** before a run. XynClaim only clicks while Roblox is the Windows foreground app. If you click another app during a run, the run stops safely, and anything not claimed yet stays for the next run.

Choose the number of character slots available on the account, from 1 to 12, then select the slots to process. Buttons above that configured count are disabled. If a selected in-range slot remains on the slot-selection screen after its click, XynClaim reports it as unavailable and continues to the next slot. That covers common empty, unused, locked, or unbought-slot outcomes, but XynClaim cannot yet name which one DBOG showed.

## Server access

- **My server** preserves the private-network code that DBOG fills in for the owner. XynClaim never clicks **Create Code**.
- **Shared code** replaces the field with a friend’s code for the active run. The code is held in memory only and masked in the UI. XynClaim clears a visible code field during normal cleanup; if Windows focus or game state makes that impossible, the run reports an explicit cleanup error instead of claiming it succeeded.
- **Public last resort** is off by default. When explicitly enabled, XynClaim attempts public-server entry only after private access was rejected. It never silently falls back to public.

DBOG’s October 2025 private-network announcement says universal codes work across worlds and remain active until manually regenerated. In practice a shared code can still fail after the owner regenerates it, loses ownership, or when DBOG rejects the join. XynClaim clears a rejected shared code from the game field, saves a redacted diagnostic, stops the remaining private-route slots unless public fallback was explicitly enabled, and never rotates a friend’s code.

## Daily results and schedules

Press **Start run** or the Start / Stop key (`F6` by default, works from any window; change or turn it off in Settings). Clicking away from Roblox during a run stops it. By default Roblox stays open; **Close when done** closes only the Roblox process attached to this run.

The **Schedule** screen can register a Windows Task Scheduler task named `XynClaim Daily` that starts XynClaim a set number of minutes after the daily reset (30 by default) and runs your saved slots in the compact pill. It only runs while you're signed in. With catch-up on, a missed time runs as soon as the PC is back. If Roblox is busy, XynClaim waits up to 2 hours or skips, whichever you chose. Uninstalling removes the task.

The result for a normal closed daily panel is **claimed, unverified**. XynClaim saves the receipt region locally but does not pretend it read every reward. It also recognizes DBOG’s visible **Global reward locked** receipt and records `claimed_global_locked` with the observed `+10 Mastery Points` fallback separately from the expected streak reward.

The planner never skips by weekday and does not guess what a missed day does. Each slot has one explicit mode:

- **Claim** claims the slot every day.
- **Align** keeps the slot on the same reward day as another slot (by default the first slot set to Claim). It claims only on days when both are on the same day and skips the rest. After a skipped day DBOG may give a grace day or offer a Robux restore, which XynClaim never buys. Otherwise the slot drops back to Day 1 and waits until the other slot is on Day 1 too, which can take up to two weeks once; after that they claim together. The day is read live from the game each time, never guessed.
- **Hold** never claims that slot until you change it.

New setups start with slot 1 on Claim and the other slots aligned with it. **Align other slots with slot 1** restores that.

Optional run controls can mute only Roblox, persist Roblox graphics quality 1 for the next launch, and temporarily lower Roblox CPU priority. Audio and CPU priority are restored after the run. If Roblox is already open, the graphics change waits for its next launch rather than rewriting a file the live client may overwrite.

The footer shows the **reported** daily-login reset as 8 PM US Eastern, converted to UTC and your PC’s local time. It is community-sourced, not an official timing guarantee.

Daily rewards require the DBOG game-group eligibility described in the game’s patch notes. The official reward table reaches Day 14, then community reports say the cycle repeats. Day-14 and multi-slot reward behavior is affected by known reports of global locking, so treat projections and expected rewards as guidance, not a guarantee.

## Local files and optional notifications

Mutable data is stored under `%LOCALAPPDATA%\XynClaim`:

- `reports\run_*.json` and `run_*.txt` — one local report per run
- `reports\history.jsonl` — append-only local run history
- `reports\receipt_*.png` and redacted diagnostic images
- `claim_state.json` — last observed result by slot
- `settings.json` — sequence, run-environment, and notification preferences

The calendar and planner are local. Optional Discord notifications are disabled by default. When enabled, the webhook URL is protected for the current Windows account with DPAPI; XynClaim accepts only HTTPS Discord webhook hosts, sends no mentions, and does not extract or send Roblox usernames, codes, screenshots, receipt images, or full reports. See [PRIVACY.md](PRIVACY.md).

## Headless use

These commands need the source checkout and Python 3.11+, not the installed app.

Owner server:

```powershell
python -m xynclaim.engine --run --slots 1-12 --slot-count 12
```

Shared code without placing it in command-line arguments or shell history:

```powershell
$code = Read-Host "Shared server code"
$code | python -m xynclaim.engine --run --slots 1-3 --slot-count 3 --shared-code-stdin
```

Add `--public-fallback` only if public-server entry is acceptable after a rejected private join. Run `python -m xynclaim.engine --help` for all options.

## Limits

- Windows only; Roblox must remain visible and focused.
- Only a 1920 × 1080 Roblox client area is supported.
- Slot count is configured, not automatically counted from DBOG’s UI.
- DBOG UI changes can invalidate references. XynClaim stops on an unknown state instead of continuing with blind input.
- The app can classify the known global-lock receipt, but it does not OCR every reward toast.
- DBOG’s missed-day behavior and exact reset timing are not confirmed by official documentation.

XynClaim is an unofficial community tool. It is not affiliated with Roblox or the Dragon Generations developers. Automating game input may be against Roblox's Terms of Use; use it at your own risk.

## Reporting a problem

Open an issue on GitHub with what happened and attach `%LOCALAPPDATA%\XynClaim\reports\live_run.log` and the newest `run_*.txt` from the same folder. Private server codes are already blanked out in these files, but read them before posting. Only download XynClaim from this repository's Releases page; each release lists the installer's SHA256 so you can check your download.

## License

MIT. See [LICENSE](LICENSE).
