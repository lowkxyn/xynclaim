import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const script = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

// Local-only page: no network, bridge allowed.
assert.match(html, /Content-Security-Policy/);
assert.match(html, /default-src 'self'/);
assert.match(html, /script-src 'self' 'unsafe-eval'/);
assert.match(html, /connect-src 'none'/);
assert.doesNotMatch(html, /mock_api/);
assert.doesNotMatch(html, / style="/, "inline styles are blocked by the CSP; set them from app.js");
assert.match(html, /pywebview-drag-region/);
assert.match(html, /<header class="titlebar pywebview-drag-region">/);
assert.match(html, /class="pill pywebview-drag-region"/);
assert.match(script, /\.tb-icons, \.pill button[\s\S]*stopPropagation/);

for (const view of ["dashboard", "streak", "history", "stats", "schedule", "settings"]) {
  assert.match(html, new RegExp(`id="view-${view}"`));
  assert.match(html, new RegExp(`href="#${view}"`));
}
for (const id of ["settingsWarning", "startButton", "stopButton", "slotTiles", "logFeed", "track", "streakTable",
  "runList", "rxList", "receiptImage", "statTotals", "claimsChart", "durationChart", "collected", "topItems",
  "slotStats", "exportCsv", "schedEnabled", "schedOffset", "schedCatchUp", "schedDay1", "schedBusy",
  "notificationsEnabled", "webhookUrl", "testNotification", "setupChecks", "accessMode", "serverCode",
  "publicFallback", "slotCount", "slotChips", "runMonitor", "muteRoblox", "lowGraphics", "lowCpuPriority",
  "closeWhenDone", "pill", "pillStop", "pillSegs"]) {
  assert.match(html, new RegExp(`id="${id}"`), `missing #${id}`);
}
for (const id of ["hotkeySetting", "hotkeyButton", "perSlotSection", "statsSlotPicker", "slotDetails",
  "slotDetailTotals", "slotCollected", "slotItems", "slotRecent", "exportSlotCsv"]) {
  assert.match(html, new RegExp(`id="${id}"`), `missing #${id}`);
}
assert.equal((styles.match(/var\(--display\)/g) ?? []).length, 1, "Chakra Petch is reserved for the brand");
assert.match(styles, /\.page h1\s*\{[^}]*700 24px\/1\.2 var\(--ui\); letter-spacing: -\.5px/s);
assert.match(styles, /\.big\s*\{[^}]*600 13px var\(--ui\); letter-spacing: normal/s);
assert.match(styles, /\.stat strong\s*\{[^}]*700 20px\/1\.25 var\(--mono\)/s);
assert.match(styles, /\.day\s*\{[^}]*min-height: 148px/s);
assert.match(styles, /@media \(max-width: 1000px\)[\s\S]*\.track \{ grid-template-columns: repeat\(7, minmax\(0, 1fr\)\)/);
assert.match(html, /id="serverCode" type="password"/);
assert.match(html, /data-window="compact"/);
assert.match(html, /data-window="expand"/);
assert.match(html, /data-window="toggle-maximize"/);

assert.doesNotMatch(script, /innerHTML/);
for (const call of ["get_state", "start_run", "stop_run", "save_plan", "save_notifications", "test_notification",
  "reset_settings", "consume_last_error", "window_action", "get_stats", "export_stats_csv", "get_streak",
  "get_run", "get_receipt", "open_reports_folder", "setup_checks", "save_run_prefs", "get_schedule",
  "save_schedule", "get_launch_info", "open_discord"]) {
  assert.match(script, new RegExp(call), `app.js never calls ${call}`);
}
for (const field of ["slot_count:", "public_fallback:", "sequence_modes:", "sequence_align_targets:", "run_monitor:",
  "mute_roblox:", "low_graphics:", "low_cpu_priority:", "offset_minutes:", "catch_up:", "roblox_busy:"]) {
  assert.match(script, new RegExp(field));
}
assert.match(script, /event\.key !== "F6"/);
assert.match(script, /document\.activeElement/);
assert.match(script, /stopping-before-close/);
assert.match(script, /received_reward/);
assert.match(script, /\$\("#serverCode"\)\.value = ""/, "the shared code is cleared once a run starts");
assert.match(script, /webhookUrl.*addEventListener\("paste"/s);
assert.match(script, /pywebviewready/);

assert.match(styles, /\.pywebview-drag-region/);
assert.match(styles, /prefers-reduced-motion/);
assert.match(styles, /\[hidden\]\{display:none!important\}/);
assert.match(styles, /body\.compact/);

console.log("ui contract checks passed");

const appRuntime = () => {
  const elements = new Map();
  const events = new Map();
  const timers = new Map();
  let timerId = 0;
  class Element {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.dataset = {};
      this.attributes = new Map();
      this.listeners = new Map();
      this.style = { setProperty() {} };
      this.parent = null;
      const classes = new Set();
      this.classList = {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, on = !classes.has(name)) => { on ? classes.add(name) : classes.delete(name); return on; },
      };
      Object.defineProperty(this, "className", {
        get: () => [...classes].join(" "),
        set: (value) => { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name)); },
      });
      this.value = "";
      this.textContent = "";
      this.disabled = false;
      this.scrollTop = this.clientHeight = this.scrollHeight = 0;
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name); }
    toggleAttribute() {}
    append(...children) {
      for (const child of children) if (child instanceof Element) child.parent = this;
      this.children.push(...children);
    }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
    // Only ".class" selectors, which is all app.js asks an element for.
    querySelector(selector) {
      for (const child of this.children) {
        if (!(child instanceof Element)) continue;
        if (child.classList.contains(selector.slice(1))) return child;
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    }
    get options() { return this.children; }
    querySelectorAll() { return []; }
    addEventListener(name, handler) {
      const handlers = this.listeners.get(name) ?? [];
      handlers.push(handler);
      this.listeners.set(name, handlers);
    }
    async dispatch(name, event = {}) {
      return Promise.all((this.listeners.get(name) ?? []).map((handler) => handler(event)));
    }
    click() { if (!this.disabled) return this.dispatch("click"); }
  }
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const document = {
    querySelector: element, querySelectorAll: () => [], getElementById: (id) => element(`#${id}`),
    createElement: (tag) => new Element(tag), createTextNode: (text) => text,
    body: new Element("body"), activeElement: null,
    addEventListener: (name, handler) => element("document").addEventListener(name, handler),
  };
  const setTimer = (callback, delay, interval = false) => {
    const id = ++timerId;
    timers.set(id, { callback, delay, interval });
    return id;
  };
  const addEventListener = (name, handler) => {
    const handlers = events.get(name) ?? [];
    handlers.push(handler);
    events.set(name, handlers);
  };
  const context = vm.createContext({
    document, location: { hash: "#dashboard" }, addEventListener,
    setTimeout: setTimer, clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, delay) => setTimer(callback, delay, true), clearInterval: (id) => timers.delete(id),
    Option: class extends Element {
      constructor(text, value) { super("option"); this.textContent = text; this.value = value; }
    },
    window: { addEventListener, setTimeout: setTimer },
    requestAnimationFrame: (callback) => callback(),
  });
  const exposed = script.replace(/\}\)\(\);\s*$/, "globalThis.app = { state, update, poll, start, selectRun, selectReceipt, renderSchedule, updateResetClock, loadChecks, loadStats, loadSlotStats, refreshSlotStats, renderStreak, renderRuns };\n})();");
  vm.runInContext(exposed, context);
  return { context, app: context.app, element, timers, events,
    bridge: (api) => { context.window.pywebview = { api }; },
    dispatch: async (name) => Promise.all((events.get(name) ?? []).map((handler) => handler())),
  };
};

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("poll never overlaps an outstanding state read", async () => {
  const runtime = appRuntime();
  const read = deferred();
  let calls = 0;
  runtime.bridge({ get_state: () => { calls += 1; return read.promise; } });
  const first = runtime.app.poll();
  const second = runtime.app.poll();
  assert.equal(calls, 1);
  read.resolve({ running: false });
  await Promise.all([first, second]);
  assert.equal(runtime.timers.size, 1);
});

test("unload cancels timers and a late state response cannot restart polling", async () => {
  const runtime = appRuntime();
  const read = deferred();
  runtime.bridge({ get_state: () => read.promise });
  const polling = runtime.app.poll();
  await runtime.dispatch("beforeunload");
  read.resolve({ running: true });
  await polling;
  assert.equal(runtime.timers.size, 0);
  assert.equal(runtime.app.state.running, false);
});

test("null and rejected state calls continue polling without JS errors", async () => {
  const runtime = appRuntime();
  runtime.bridge({ get_state: () => Promise.reject(new Error("unavailable")) });
  await runtime.app.poll();
  runtime.bridge({ get_state: () => null });
  await runtime.app.poll();
  assert.equal(runtime.timers.size, 1);
});

test("F6 cannot double-start while the start response is pending", async () => {
  const runtime = appRuntime();
  const starting = deferred();
  let calls = 0;
  runtime.bridge({ start_run: () => { calls += 1; return starting.promise; } });
  const key = { key: "F6", repeat: false, preventDefault() {} };
  await runtime.element("document").dispatch("keydown", key);
  await runtime.element("document").dispatch("keydown", key);
  assert.equal(calls, 1);
  starting.resolve(null);
  await new Promise(setImmediate);
  assert.equal(runtime.element("#startButton").disabled, false);
});

test("empty start responses leave the start button usable", async () => {
  const runtime = appRuntime();
  runtime.bridge({ start_run: () => undefined });
  await runtime.element("#startButton").click();
  assert.equal(runtime.app.state.running, false);
  assert.equal(runtime.element("#startButton").disabled, false);
});

test("a state read started before a new run cannot overwrite the new run", async () => {
  const runtime = appRuntime();
  const read = deferred();
  runtime.bridge({ get_state: () => read.promise, start_run: () => ({ started: true, slots: [1] }) });
  const polling = runtime.app.poll();
  await runtime.element("#startButton").click();
  read.resolve({ running: false, report: { slots: [{ slot: 2 }] } });
  await polling;
  assert.equal(runtime.app.state.running, true);
  assert.equal(runtime.app.state.report, null);
});

test("a new empty report clears the previous run and active slots", () => {
  const runtime = appRuntime();
  runtime.app.update({ running: false, report: { slots: [{ slot: 1 }] }, active_slots: [1] });
  runtime.app.update({ running: false, report: null, live_results: [], active_slots: [] });
  assert.equal(runtime.app.state.report, null);
  assert.equal(runtime.app.state.activeSlots.length, 0);
});

test("a late run response cannot replace a newer selection", async () => {
  const runtime = appRuntime();
  const oldRun = deferred();
  runtime.bridge({ get_run: (stamp) => stamp === "old" ? oldRun.promise : { started_utc: "new", slots: [] } });
  const selecting = runtime.app.selectRun("old");
  await runtime.app.selectRun("new");
  const title = runtime.element("#detailTitle").textContent;
  oldRun.resolve({ started_utc: "2026-09-28T12:00:00Z", slots: [] });
  await selecting;
  assert.equal(runtime.element("#detailTitle").textContent, title);
});

test("a late receipt for the same slot in another run cannot replace the selection", async () => {
  const runtime = appRuntime();
  const receipt = deferred();
  runtime.bridge({ get_receipt: (stamp) => stamp === "old" ? receipt.promise : Promise.resolve({ data_uri: "new image" }) });
  runtime.app.state.selectedRun = "old";
  const selecting = runtime.app.selectReceipt("old", { slot: 1 });
  runtime.app.state.selectedRun = "new";
  await runtime.app.selectReceipt("new", { slot: 1 });
  receipt.resolve({ data_uri: "old image" });
  await selecting;
  assert.equal(runtime.element("#receiptImage").src, "new image");
});

test("a synchronous null receipt is handled without JS errors", async () => {
  const runtime = appRuntime();
  runtime.bridge({ get_receipt: () => null });
  runtime.app.state.selectedRun = "run";
  await runtime.app.selectReceipt("run", { slot: 1 });
  assert.equal(runtime.element("#receiptImage").hidden, true);
});

test("reset countdown uses the next reset instant across a DST change", () => {
  const runtime = appRuntime();
  runtime.context.Date = class extends Date {
    constructor(value) { super(value ?? "2026-10-25T00:10:00Z"); }
  };
  runtime.app.renderSchedule({ reset_local: "01:00", next_reset_utc: "2026-10-26T00:00:00Z" });
  assert.match(runtime.element("#resetClock").textContent, /in 23h 50m$/);
});

test("an expired reset is refreshed once instead of reusing yesterday's local hour", async () => {
  const runtime = appRuntime();
  let calls = 0;
  runtime.context.Date = class extends Date {
    constructor(value) { super(value ?? "2026-10-25T00:10:00Z"); }
  };
  runtime.bridge({ get_schedule: () => {
    calls += 1;
    return { reset_local: "00:00", next_reset_utc: "2026-10-26T00:00:00Z" };
  } });
  runtime.app.renderSchedule({ reset_local: "01:00", next_reset_utc: "2026-10-25T00:00:00Z" });
  runtime.app.updateResetClock();
  await new Promise(setImmediate);
  assert.equal(calls, 1);
  assert.match(runtime.element("#resetClock").textContent, /in 23h 50m$/);
});

const feedLines = (runtime) => runtime.element("#logFeed").children
  .filter((line) => !line.classList.contains("placeholder")).map((line) => line.textContent);

test("run state follows status: paused, stopping, then done stays until the next start", () => {
  const runtime = appRuntime();
  const frame = runtime.element("#windowFrame");
  runtime.app.update({ running: true, status: "claiming", active_slots: [1, 2], current_slot: 1 });
  assert.equal(frame.dataset.runState, "running");
  runtime.app.update({ running: true, status: "paused", paused_reason: "Roblox isn't in front." });
  assert.equal(frame.dataset.runState, "paused");
  assert.equal(runtime.element("#nowText").textContent, "Roblox isn't in front.");
  assert.equal(runtime.element("#nowDot").className, "dot warn");
  runtime.app.update({ running: true, status: "stopping" });
  assert.equal(runtime.element("#stopButton").disabled, true);
  assert.equal(runtime.element("#stopButton").textContent, "STOPPING…");
  const report = { slots: [{ slot: 1, status: "claimed_verified" }, { slot: 2, status: "failed" }] };
  runtime.app.update({ running: false, status: "needs_attention", report });
  assert.equal(frame.dataset.runState, "attention");
  assert.equal(runtime.element("#nowText").textContent, "Needs attention: 1 claimed, 1 issue");
  assert.equal(runtime.element("#stopButton").textContent, "STOP");
  runtime.app.update({ running: false, status: "needs_attention", report });
  assert.equal(frame.dataset.runState, "attention");
});

test("a failed run keeps its error on screen after the backend clears it", () => {
  const runtime = appRuntime();
  runtime.app.update({ running: false, status: "failed", error: "Roblox closed" });
  runtime.app.update({ running: false, status: "failed", error: "" });
  assert.equal(runtime.element("#windowFrame").dataset.runState, "failed");
  assert.equal(runtime.element("#nowText").textContent, "Failed: Roblox closed");
});

test("the log feed appends new lines, follows the 250-line shift and restarts on a new run", () => {
  const runtime = appRuntime();
  runtime.app.update({ running: true, logs: ["a", "b"] });
  const first = runtime.element("#logFeed").children[0];
  runtime.app.update({ running: true, logs: ["a", "b", "c"] });
  assert.equal(runtime.element("#logFeed").children[0], first, "existing lines are kept, not rebuilt");
  assert.deepEqual(feedLines(runtime), ["a", "b", "c"]);
  runtime.app.update({ running: true, logs: ["b", "c", "d"] });
  assert.deepEqual(feedLines(runtime), ["a", "b", "c", "d"]);
  runtime.app.update({ running: true, logs: ["new run"] });
  assert.deepEqual(feedLines(runtime), ["new run"]);
  runtime.app.update({ running: true, logs: [] });
  assert.deepEqual(feedLines(runtime), []);
});

test("slot tiles and chips are built once and patched in place", () => {
  const runtime = appRuntime();
  runtime.app.update({ running: false, settings: { slot_count: 3, slots: [1, 2, 3] } });
  const tiles = runtime.element("#slotTiles").children;
  const chips = runtime.element("#slotChips").children;
  assert.equal(tiles.length, 12);
  assert.equal(tiles.filter((tile) => !tile.hidden).length, 3);
  runtime.app.update({ running: true, status: "claiming", active_slots: [1, 2, 3], current_slot: 2 });
  assert.equal(runtime.element("#slotTiles").children, tiles);
  assert.equal(runtime.element("#slotChips").children, chips);
  assert.equal(tiles[1].className, "slot live");
  assert.equal(chips[0].disabled, true);
});

test("the global hotkey start request takes the START path once", async () => {
  const runtime = appRuntime();
  let starts = 0;
  runtime.bridge({ start_run: () => { starts += 1; return null; } });
  runtime.app.update({ running: false, hotkey_start_requested: true, hotkey: "Ctrl+Alt+X" });
  assert.equal(runtime.element("#hotkeyLabel").textContent, "Ctrl+Alt+X");
  await new Promise(setImmediate);
  runtime.app.update({ running: false });
  await new Promise(setImmediate);
  assert.equal(starts, 1);
  runtime.app.update({ running: true, hotkey_start_requested: true, hotkey: "" });
  await new Promise(setImmediate);
  assert.equal(starts, 1);
  assert.equal(runtime.element("#hotkeyLabel").textContent, "F6");
});

test("a failing setup check shows the dashboard banner and explains START", async () => {
  const runtime = appRuntime();
  runtime.bridge({ setup_checks: () => [
    { state: "ok", title: "Windows OCR", detail: "fine" },
    { state: "bad", title: "Roblox installed", detail: "Install Roblox for this Windows user" },
  ] });
  await runtime.app.loadChecks();
  assert.equal(runtime.element("#setupBanner").hidden, false);
  assert.match(runtime.element("#startButton").title, /Install Roblox for this Windows user/);
});

const press = (runtime, key, modifiers = {}) => runtime.element("document").dispatch("keydown", {
  key, repeat: false, preventDefault() {}, ...modifiers,
});
const textOf = (element) => typeof element === "string" ? element
  : [element.textContent, ...element.children.map(textOf)].join(" ");

test("registered global keys disable the page F6 fallback, including when running", async () => {
  const runtime = appRuntime();
  let calls = 0;
  runtime.bridge({ start_run: () => { calls += 1; }, stop_run: () => { calls += 1; } });
  for (const key of ["F6", "Ctrl+Alt+X"]) {
    runtime.app.update({ hotkey: key, running: false });
    await press(runtime, "F6");
    runtime.app.update({ hotkey: key, running: true });
    await press(runtime, "F6");
  }
  assert.equal(calls, 0);
  runtime.app.update({ hotkey: "", running: false });
  await press(runtime, "F6");
  assert.equal(calls, 1);
});

test("key capture saves function keys and modifier combinations without starting", async () => {
  const runtime = appRuntime();
  const saved = [];
  let starts = 0;
  runtime.bridge({ save_hotkey: (key) => { saved.push(key); return { hotkey: key }; }, start_run: () => { starts += 1; } });
  for (const [key, modifiers, expected] of [
    ["F6", {}, "F6"], ["F12", {}, "F12"],
    ["x", { ctrlKey: true, altKey: true }, "Ctrl+Alt+X"],
    ["S", { ctrlKey: true, shiftKey: true }, "Ctrl+Shift+S"],
    ["!", { shiftKey: true, code: "Digit1" }, "Shift+1"],
  ]) {
    await runtime.element("#hotkeyButton").click();
    assert.equal(runtime.element("#hotkeyButton").textContent, "Press a key...");
    await press(runtime, key, modifiers);
    assert.equal(saved.at(-1), expected);
    assert.equal(runtime.element("#hotkeyButton").textContent, expected);
    assert.equal(runtime.element("#hotkeyLabel").textContent, expected);
    assert.equal(runtime.element("#hotkeyText").textContent, "Start / Stop, works in game");
  }
  assert.equal(starts, 0);
  assert.match(runtime.element("#toast").textContent, /saved/);
});

test("capture rejects bare letters, unsupported keys and repeats; Escape and blur cancel", async () => {
  const runtime = appRuntime();
  let saves = 0;
  runtime.bridge({ save_hotkey: () => { saves += 1; } });
  await runtime.element("#hotkeyButton").click();
  for (const key of ["x", "1", "F13", "Enter", "Control", "Alt", "Shift"]) await press(runtime, key);
  await press(runtime, "F9", { repeat: true });
  await press(runtime, "x", { metaKey: true, ctrlKey: true });
  assert.equal(runtime.app.state.hotkeyCapturing, true);
  await press(runtime, "Escape");
  assert.equal(runtime.app.state.hotkeyCapturing, false);
  await runtime.element("#hotkeyButton").click();
  await runtime.dispatch("blur");
  assert.equal(runtime.app.state.hotkeyCapturing, false);
  assert.equal(saves, 0);
});

test("Backspace and Delete turn the global key off and retain the focused fallback", async () => {
  const runtime = appRuntime();
  const saved = [];
  runtime.bridge({ save_hotkey: (key) => { saved.push(key); return { hotkey: "" }; } });
  for (const key of ["Backspace", "Delete"]) {
    runtime.app.update({ hotkey: "F9" });
    await runtime.element("#hotkeyButton").click();
    await press(runtime, key);
    assert.equal(runtime.app.state.hotkey, "");
    assert.equal(runtime.element("#hotkeyButton").textContent, "Off");
    assert.equal(runtime.element("#hotkeyLabel").textContent, "F6");
  }
  assert.deepEqual(saved, ["", ""]);
});

test("pending key saves block capture and stale polls cannot restore the old key", async () => {
  const runtime = appRuntime();
  const saving = deferred();
  const read = deferred();
  let saves = 0;
  runtime.bridge({ get_state: () => read.promise, save_hotkey: () => { saves += 1; return saving.promise; } });
  const polling = runtime.app.poll();
  await runtime.element("#hotkeyButton").click();
  const capturing = press(runtime, "F9");
  await runtime.element("#hotkeyButton").click();
  await press(runtime, "F6");
  assert.equal(runtime.element("#hotkeyButton").disabled, true);
  saving.resolve({ hotkey: "F9" });
  await capturing;
  read.resolve({ hotkey: "F6" });
  await polling;
  assert.equal(saves, 1);
  assert.equal(runtime.app.state.hotkey, "F9");
});

test("older or failing backends hide the new settings and details", async () => {
  const runtime = appRuntime();
  runtime.bridge({ get_stats: () => ({}) });
  runtime.app.update({});
  assert.equal(runtime.element("#hotkeySetting").hidden, true);
  await runtime.app.loadSlotStats(1);
  assert.equal(runtime.element("#perSlotSection").hidden, true);
  runtime.bridge({ get_stats: () => ({}), save_hotkey: () => { throw new Error("Key already in use"); }, get_slot_stats: () => { throw new Error("unsupported"); } });
  runtime.app.update({ hotkey: "F6" });
  assert.equal(runtime.element("#hotkeySetting").hidden, false);
  await runtime.element("#hotkeyButton").click();
  await press(runtime, "F9");
  assert.equal(runtime.element("#hotkeySetting").hidden, true);
  assert.match(runtime.element("#toast").textContent, /Key already in use/);
  await runtime.app.loadStats();
  await new Promise(setImmediate);
  assert.equal(runtime.element("#perSlotSection").hidden, true);
  assert.equal(runtime.element("#statTotals").children.length, 4);
});

const slotStats = (slot) => ({
  slot, claims: 12, verified: 10, verified_pct: 83.3, current_day: 7, longest_streak: 14,
  last_claimed_utc: "2026-10-19T12:00:00Z", avg_seconds: 42.5,
  collected: { zeni: 12000, mastery_points: 20, xp_boost_minutes: 60, dragon_balls: 2, dragon_balls_unique: 2 },
  items: [["<item & reward>", 3]], recent: [{ date: "2026-10-19", day: 7, status: "claimed_verified", received_reward: "<reward>", seconds: 42.5 }],
});

test("slot picker follows slot_count and renders expandable details, rewards and claims", async () => {
  const runtime = appRuntime();
  runtime.app.state.prefs.slot_count = 3;
  runtime.bridge({ get_slot_stats: slotStats });
  await runtime.app.refreshSlotStats();
  assert.equal(runtime.element("#statsSlotPicker").options.length, 3);
  runtime.element("#statsSlotPicker").value = "2";
  await runtime.element("#statsSlotPicker").dispatch("change");
  assert.equal(runtime.element("#slotDetails").open, true);
  assert.equal(runtime.element("#slotDetailsTitle").textContent, "Slot 2 details");
  assert.equal(runtime.element("#perSlotSection").hidden, false);
  assert.equal(runtime.element("#slotDetailTotals").children.length, 5);
  assert.equal(runtime.element("#slotCollected").children.length, 8);
  assert.match(textOf(runtime.element("#slotCollected")), /12,000/);
  assert.match(textOf(runtime.element("#slotItems")), /<item & reward>/);
  assert.match(textOf(runtime.element("#slotRecent")), /claimed verified.*<reward>.*42\.5 s/);
  runtime.app.state.prefs.slot_count = 1;
  await runtime.app.refreshSlotStats();
  assert.equal(runtime.app.state.statsSlot, 1);
  assert.equal(runtime.element("#statsSlotPicker").value, "1");
});

test("late slot responses and failures cannot replace a newer selection", async () => {
  for (const fail of [false, true]) {
    const runtime = appRuntime();
    const old = deferred();
    runtime.bridge({ get_slot_stats: (slot) => slot === 1 ? old.promise : slotStats(slot) });
    const loading = runtime.app.loadSlotStats(1);
    await runtime.app.loadSlotStats(2);
    fail ? old.reject(new Error("old request failed")) : old.resolve(slotStats(1));
    await loading;
    assert.equal(runtime.element("#slotDetailsTitle").textContent, "Slot 2 details");
    assert.equal(runtime.element("#perSlotSection").hidden, false);
  }
});

test("settings arriving on the statistics page update the picker to the actual slot count", async () => {
  const runtime = appRuntime();
  runtime.context.location.hash = "#stats";
  runtime.bridge({ get_slot_stats: slotStats });
  runtime.app.update({ settings: { slot_count: 3, slots: [1, 2, 3] } });
  await new Promise(setImmediate);
  assert.equal(runtime.element("#statsSlotPicker").options.length, 3);
});

test("empty slot stats render useful empty states and exports use the right argument", async () => {
  const runtime = appRuntime();
  const exports = [];
  runtime.bridge({ get_slot_stats: (slot) => ({ slot }), export_stats_csv: (...args) => {
    exports.push(args); return { path: "C:\\reports\\slot-3.csv" };
  } });
  await runtime.app.loadSlotStats(3);
  assert.match(textOf(runtime.element("#slotItems")), /No item rewards/);
  assert.match(textOf(runtime.element("#slotRecent")), /No recent claims/);
  assert.match(runtime.element("#slotLastClaim").textContent, /No claims recorded/);
  await runtime.element("#exportSlotCsv").click();
  assert.equal(runtime.element("#toast").textContent, "Saved slot-3.csv");
  await runtime.element("#exportCsv").click();
  assert.deepEqual(exports, [[3], []]);
});


test("Align offers existing target slots except itself", () => {
  const runtime = appRuntime();
  runtime.app.state.prefs.slot_count = 3;
  runtime.app.state.streak = { slots: [1, 2, 3].map((slot) => ({ slot, mode: slot === 1 ? "always" : "align", align_target: 1 })) };
  runtime.app.renderStreak();
  const rows = runtime.element("#streakTable").children.slice(1);
  assert.deepEqual(Array.from(rows[1].children[4].children[1].options, (option) => option.textContent), ["with slot 1", "with slot 3"]);
  assert.equal(rows[0].children[4].children.length, 1);
  assert.deepEqual(Array.from(rows[2].children[4].children[1].options, (option) => option.value), ["1", "2"]);
});

test("rule changes and target changes share the debounced autosave", async () => {
  const runtime = appRuntime();
  const saves = [];
  runtime.bridge({ save_plan: (plan) => { saves.push(plan); return { saved: true }; } });
  runtime.app.state.prefs.slot_count = 3;
  runtime.app.state.streak = { slots: [{ slot: 2, mode: "always" }] };
  runtime.app.renderStreak();
  const rule = () => runtime.element("#streakTable").children[1].children[4];
  await rule().children[0].dispatch("change", { target: { value: "align" } });
  await rule().children[1].dispatch("change", { target: { value: "3" } });
  assert.equal(saves.length, 0);
  const pending = [...runtime.timers.values()].filter((timer) => timer.delay === 300);
  assert.equal(pending.length, 1);
  await pending[0].callback();
  assert.equal(saves.length, 1);
  assert.equal(saves[0].sequence_modes[2], "align");
  assert.equal(saves[0].sequence_align_targets[2], 3);
  assert.equal(Object.keys(saves[0].sequence_modes).length, 3);
  assert.equal(saves[0].sequence_targets, undefined);
  await rule().children[0].dispatch("change", { target: { value: "hold" } });
  assert.equal(rule().children.length, 1);
});

test("one-slot settings cannot select Align and preset saves no missing target", async () => {
  const runtime = appRuntime();
  let saved;
  runtime.bridge({ save_plan: (plan) => { saved = plan; } });
  runtime.app.state.prefs.slot_count = 1;
  runtime.app.state.streak = { slots: [{ slot: 1, mode: "always" }] };
  runtime.app.renderStreak();
  const choices = runtime.element("#streakTable").children[1].children[4].children[0].options;
  assert.deepEqual(Array.from(choices, (option) => option.value), ["always", "hold"]);
  await runtime.element("#sequencePreset").click();
  await [...runtime.timers.values()].find((timer) => timer.delay === 300).callback();
  assert.deepEqual(Object.keys(saved.sequence_align_targets), []);
  assert.equal(saved.sequence_modes[1], "always");
});

test("preset aligns configured slots with slot one", async () => {
  const runtime = appRuntime();
  runtime.app.state.prefs.slot_count = 3;
  runtime.app.state.streak = { slots: [{ slot: 1 }, { slot: 2 }, { slot: 3 }] };
  await runtime.element("#sequencePreset").click();
  assert.equal(runtime.app.state.plan[1].mode, "always");
  assert.equal(runtime.app.state.plan[2].target, 1);
  assert.equal(runtime.app.state.plan[3].target, 1);
  assert.equal(runtime.app.state.plan[4], undefined);
});

test("slot Dragon Balls display quantities and clear when absent", async () => {
  const runtime = appRuntime();
  runtime.bridge({ get_slot_stats: (slot) => ({ ...slotStats(slot), dragon_balls_detail: [["1-Star", 1], ["3-Star", 2], ["7-Star", 1]] }) });
  await runtime.app.loadSlotStats(1);
  assert.equal(runtime.element("#slotDragonBalls").textContent, "1-Star, 3-Star x2, 7-Star");
  runtime.bridge({ get_slot_stats: (slot) => ({ slot, dragon_balls_detail: [] }) });
  await runtime.app.loadSlotStats(2);
  assert.equal(runtime.element("#slotDragonBalls").textContent, "No Dragon Balls read yet.");
  runtime.bridge({ get_slot_stats: (slot) => ({ slot }) });
  await runtime.app.loadSlotStats(3);
  assert.equal(runtime.element("#slotDragonBalls").textContent, "No Dragon Balls read yet.");
});

test("Align slots follow the main Claim slot when it moves, but keep other picks", async () => {
  const runtime = appRuntime();
  runtime.bridge({ save_plan: () => {} });
  runtime.app.state.prefs.slot_count = 4;
  runtime.app.state.streak = { slots: [1, 2, 3, 4].map((slot) => ({ slot })) };
  Object.assign(runtime.app.state.plan, {
    1: { mode: "always" }, 2: { mode: "align", target: 1 }, 3: { mode: "align", target: 1 }, 4: { mode: "align", target: 2 },
  });
  runtime.app.renderStreak();
  const ruleSelect = (slot) => runtime.element("#streakTable").children[slot].children[4].children[0];
  // Slot 3 becomes the Claim slot, slot 1 is held: slot 2 now follows slot 3, slot 4 keeps its own pick.
  await ruleSelect(3).dispatch("change", { target: { value: "always" } });
  await ruleSelect(1).dispatch("change", { target: { value: "hold" } });
  assert.equal(runtime.app.state.plan[2].target, 3);
  assert.equal(runtime.app.state.plan[4].target, 2);
  // A slot switched to Align starts by following the main Claim slot.
  await ruleSelect(1).dispatch("change", { target: { value: "align" } });
  assert.equal(runtime.app.state.plan[1].target, 3);
  // With no Claim slot left, targets stay as they were.
  await ruleSelect(3).dispatch("change", { target: { value: "hold" } });
  assert.equal(runtime.app.state.plan[2].target, 3);
});

test("history groups runs by day with a claimed total and a dropdown per day", async () => {
  const runtime = appRuntime();
  runtime.bridge({ get_run: () => null });
  const run = (started, statuses) => ({ started_utc: started, total_seconds: 60, slots: statuses.map((status, i) => ({ slot: i + 1, status })) });
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const at = (daysAgo, hour) => new Date(today.getTime() - daysAgo * 86400000 + (hour - 12) * 3600000).toISOString();
  runtime.app.state.history = [
    run(at(1, 9), ["claimed_verified", "claimed_verified"]),
    run(at(0, 9), ["claimed_verified", "failed", "not_attempted"]),
    run(at(0, 10), ["no_daily", "claimed_verified", "claimed_verified"]),
  ];
  runtime.app.renderRuns();
  const rows = () => runtime.element("#runList").children;
  assert.deepEqual(rows().map((row) => row.className.includes("run-day") ? "day" : "run"), ["day", "run", "run", "day"]);
  assert.equal(rows()[0].children[0].children[0].textContent, "Today");
  assert.equal(rows()[0].children[1].textContent, "3 claimed");
  assert.equal(rows()[3].children[1].textContent, "2 claimed");
  assert.equal(rows()[1].children[1].textContent, "2/3");
  await rows()[3].click();
  assert.equal(rows().length, 5);
  await rows()[0].click();
  assert.deepEqual(rows().map((row) => row.className.includes("run-day") ? "day" : "run"), ["day", "day", "run"]);
});

test("Streak explains all three rules and never promises paid restoration", () => {
  assert.match(html, /Claim: claims this slot every day\./);
  assert.match(html, /by default the first slot set to Claim/);
  assert.match(html, /XynClaim never buys it/);
  assert.match(html, /which can take up to two weeks once/);
  assert.match(html, /Hold: never claims this slot until you change it\./);
  assert.doesNotMatch(html, /Line slots up as Days/);
});
