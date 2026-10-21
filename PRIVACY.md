# Privacy

XynClaim runs locally. Its interface is a bundled local page hosted by the installed Microsoft Edge WebView2 runtime; it loads no remote scripts, styles, fonts, or images. It has no telemetry, analytics, account sign-in, or background service.

## Local data

XynClaim stores its reports, receipt crops, redacted diagnostics, claim state, history, planner settings, and optional notification preference under `%LOCALAPPDATA%\XynClaim`.

Reports include run timing, selected-slot results, expected/observed global-lock result information, and sanitized errors. `history.jsonl` is append-only local history. Receipt crops contain the in-game reward-notification area; diagnostic images black out fixed identity and private-code regions before they are written.

Shared private-network codes are held only in memory for an active run. They are masked in the interface, excluded from claim state, reports, history, and diagnostics, and redacted from runner log/error text. XynClaim clears the visible field during cleanup when the retained Roblox window, focus, geometry, and private-panel state can all be confirmed; otherwise it records a cleanup error. The headless shared-code option reads one code from standard input so it is not placed in command-line arguments.

Closing XynClaim during a run requests a safe stop. If the window is dismissed before the run finishes, the non-daemon runner stays alive in the background until its cleanup path completes; dismissing the window does not terminate that cleanup thread.

Delete `%LOCALAPPDATA%\XynClaim` to remove XynClaim’s saved local data. Older project-folder data may be copied once during migration; the original is not automatically removed.

## Optional Discord webhook

Discord notification is off by default. If enabled, its webhook URL is encrypted with Windows DPAPI for the current Windows account and stored in `settings.json` only as the protected value. XynClaim validates HTTPS webhook URLs and permits only `discord.com`, `canary.discord.com`, or `ptb.discord.com` webhook hosts.

The notification is a short aggregate run summary: claim count, global-lock count, no-daily count, issue count, duration, and normalized access type (`Owner private server` or `Shared private code`). It does not extract or send a Roblox username, user-entered server-profile label, private server code, screenshot, receipt image, raw report, or diagnostic. It disables mentions through Discord’s `allowed_mentions` setting.

Webhook URLs are credentials. Do not share `settings.json` or its protected blob with someone who can run it as your Windows account. Deleting the saved webhook in XynClaim, or deleting `%LOCALAPPDATA%\XynClaim`, removes the stored protected value.

## Roblox interaction

XynClaim controls the visible Roblox client through screen capture and standard Windows input. It does not inject code, inspect Roblox memory, read browser data, or collect Roblox credentials.
