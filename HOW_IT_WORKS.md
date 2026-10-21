# How XynClaim works

What the app does, step by step, what it touches on your PC, and what it sends anywhere.

## The short version

XynClaim plays the daily-reward routine the way you would: it looks at the Roblox window, clicks,
and reads what's on screen. It doesn't inject into Roblox, read its memory, or talk to Roblox or
DBOG servers. Everything it knows comes from screenshots of the Roblox window, matched against
small reference images in `assets/reference/` and read with the built-in Windows OCR.

## A run, step by step

1. **Lock and stay awake.** Only one run at a time (a Windows mutex). Windows is told not to
   sleep or turn the display off until the run ends.
2. **Find or open Roblox.** If a Roblox window exists, XynClaim attaches to it. If none exists, it
   opens Dragon Generations through the normal `roblox://` link.
3. **Size the window.** The inside of the Roblox window is set to exactly 1920×1080 on the display
   you picked (Current, Primary or Second). Your monitor resolution isn't changed. The original
   size, position and fullscreen state are put back when the run ends.
4. **Optional low graphics.** Once DBOG's main menu is up, XynClaim reads your graphics level
   from Roblox's settings file and presses Roblox's own Shift+F10 down to 1. When the run ends it
   presses F10 back up to your level. This only works with Graphics Mode on Manual; on Automatic
   it leaves graphics alone. If a run dies before raising it, a small note remembers your level
   and the next run puts it back.
5. **Optional mute.** If you turned it on, only Roblox's audio is muted, then restored after.
6. **Each selected slot:**
   1. From the main menu, click **Play**, then the slot on the slot list. If the slot stays on the
      list (empty, locked or not bought), it's reported as unavailable and the run moves on.
   2. Open **Private Network** and join. "My server" keeps the code DBOG already filled in;
      XynClaim never clicks **Create Code**. "Shared code" types a friend's code, which is held in
      memory only and masked in the UI and logs.
   3. If private access is rejected and you enabled **Public last resort**, it uses Quick Join.
      Otherwise the rejected shared code is cleared and later slots stop.
   4. Wait for the daily panel. If the world loads without one, the slot is recorded as "no daily".
   5. Read the day from the button ("Claim Day N"). The day has to read the same three times in a
      row before it's trusted, because the panel fades in.
   6. Check the day against your plan: it holds the slot if the day is outside your minimum or
      maximum. "Claim" claims the day shown. "Align" claims only if the day shown is the same day
      its target slot claims today (targets are run first); if the target's day isn't known it
      claims anyway and says so in the log. "Hold" slots never get this far.
   7. Click **Claim** once. Before the click lands it re-reads the button, and it never clicks
      again once the button says "Claimed". **Restore Streak costs Robux and is never clicked.**
   8. Confirm the streak counter went up, then read the reward popup with OCR. The result is
      saved straight away, so a later problem can't lose it.
   9. Go back to the main menu (Menu → Travel → Main Menu) for the next slot.
7. **Clean up.** Put the window and audio back, clear any shared code still on screen, and
   optionally close the Roblox process this run started or attached to.

Every click checks first that Roblox is the foreground window and that nothing covers the click
point. If you click another app, the run stops safely instead of clicking blind.

## Results you'll see per slot

| Status | Meaning |
|---|---|
| claimed verified | Claimed, and the reward popup was read |
| claimed unverified | Claim accepted, but the popup couldn't be read |
| claimed global locked | DBOG gave the "+10 Mastery Points" global-lock reward instead |
| no daily | Already claimed today, or no panel appeared |
| held | Not claimed on purpose (your min/max day rule, or an unresolved earlier click) |
| unavailable / not attempted / failed | Slot missing, run stopped early, or an error |

**Pending claims.** Just before clicking Claim, XynClaim writes down "about to claim Day N for
slot X". If the run dies right then, it doesn't know whether the click landed. Next time it
only clears that note when the panel proves it (the streak moved on). Until then the slot is held
rather than risking a double click.

## What it stores (all on your PC)

Everything lives in `%LOCALAPPDATA%\XynClaim`:

- `settings.json`: your plan and options. A Discord webhook URL is encrypted with Windows DPAPI,
  so it only decrypts under your Windows account.
- `claim_state.json`: last claimed day and reward per slot, plus any pending-claim note.
- `reports\`: one JSON report per run, `history.jsonl` (append-only, no codes), the live log,
  small receipt crops of the reward popup, and up to 10 diagnostic screenshots when something
  fails. The private-code box is blacked out in diagnostics.
- `window.json`: the app window's size and position.
- `graphics_restore.json`: only while graphics are lowered. It holds your graphics level (1-10).

Uninstalling removes the app but keeps this folder, so your history survives a reinstall.

## What it sends anywhere

Nothing, unless you add a Discord webhook. Then after each run it posts one summary: counts
(claimed, locked, no daily, issues), the duration, whether it used your server or a shared code,
and one line per slot (status, day, reward). Server codes, screenshots and receipts are never
included. Only `discord.com` webhook URLs are accepted, and redirects are refused.

## What it changes outside its own folder

- The Roblox window size/position during a run (restored afterwards).
- Roblox's audio mute during a run, if enabled (restored afterwards).
- Roblox's graphics level, if you enabled low graphics (changed with Roblox's own keys, put back
  after the run). XynClaim never writes Roblox's settings file.
- A Windows scheduled task named "XynClaim Daily", only if you turn on the schedule.
- Process control: it can close Roblox when done (if enabled), and
  before launching it force-closes a Roblox process that's been running 20+ seconds without
  any visible window. A Roblox with a window, even minimized, is never closed.

## Known limits

1. **Fixed layout.** Click points and reference images assume DBOG's current UI at 1920×1080. A
   DBOG UI update breaks runs until the references are updated (as with the October overhaul).
2. **Unsigned installer.** Windows SmartScreen warns on first run. No signing certificate planned.
3. **Sign-in is a hint, not a check.** XynClaim never reads your Roblox login, so it can't tell
   "not signed in" apart from "Roblox didn't load". Both end with the same sign-in suggestion.
