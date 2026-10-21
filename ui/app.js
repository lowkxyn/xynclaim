(() => {
  const $ = (selector) => document.querySelector(selector);
  const api = () => window.pywebview?.api;

  const state = {
    running: false,
    status: "idle",
    currentSlot: null,
    activeSlots: [],
    report: null,
    logs: [],
    history: [],
    streak: null,
    schedule: null,
    prefs: { slot_count: 12, slots: new Set(), access_mode: "owner", close_when_done: false, public_fallback: false },
    plan: {},
    settingsApplied: false,
    lastError: "",
    closeRequested: false,
    compact: false,
    selectedRun: null,
    selectedSlot: null,
    poll: null,
    pollBusy: false,
    pollRevision: 0,
    unloaded: false,
    resetTimer: null,
    scheduleRefreshBusy: false,
    starting: false,
    settingsError: false,
    historyLoaded: false,
    progress: 0,
    ticker: "",
    elapsed: "00:00",
    pausedReason: "",
    // The last run error stays on screen until the next start, after the backend has cleared it.
    runError: "",
    hotkey: "",
    hotkeyCapturing: false,
    hotkeySaving: false,
    hotkeyUnavailable: false,
    statsSlot: 1,
    slotStatsRequest: 0,
  };

  // Small DOM builder: every string goes in as text, never parsed as HTML.
  const h = (tag, props = {}, ...children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
      else element.setAttribute(key, value === true ? "" : value);
    }
    element.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
    return element;
  };

  const toast = (message, error = false) => {
    const element = $("#toast");
    element.textContent = message;
    element.classList.toggle("error", error);
    element.title = error ? "Click to dismiss" : "";
    element.classList.add("show");
    clearTimeout(toast.timer);
    // Errors stay longer so they can be read; they can be clicked away.
    toast.timer = setTimeout(() => element.classList.remove("show"), error ? 8000 : 3600);
  };

  const invoke = async (name, payload, { quiet = false } = {}) => {
    if (!api()?.[name]) {
      if (!quiet) toast("XynClaim isn't ready yet, try again", true);
      return null;
    }
    try {
      const result = payload === undefined ? await api()[name]() : await api()[name](payload);
      return result ?? null;
    } catch (error) {
      if (!quiet) toast(error?.message || String(error), true);
      return null;
    }
  };

  const pad2 = (value) => String(value).padStart(2, "0");
  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const minutes = (seconds) => `${(Number(seconds || 0) / 60).toFixed(1)} min`;
  const number = (value) => Number(value || 0).toLocaleString("en-GB");
  const claimed = (status) => String(status || "").startsWith("claimed");

  // ---------- switches, segmented controls ----------

  const setSwitch = (element, on) => element.setAttribute("aria-checked", String(Boolean(on)));
  const switchOn = (element) => element.getAttribute("aria-checked") === "true";
  const bindSwitch = (id, onChange) => {
    const element = $(id);
    setSwitch(element, false);
    element.addEventListener("click", () => {
      setSwitch(element, !switchOn(element));
      onChange?.(switchOn(element));
    });
  };

  const setSegment = (element, value) => {
    element.dataset.value = value;
    element.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("on", button.dataset.value === value);
      button.setAttribute("aria-pressed", String(button.dataset.value === value));
    });
  };
  const bindSegment = (id, onChange) => {
    const element = $(id);
    element.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        setSegment(element, button.dataset.value);
        onChange?.(button.dataset.value);
      });
    });
  };

  // ---------- run prefs (slots, server access) ----------

  const selectedSlots = () => [...state.prefs.slots].filter((slot) => slot <= state.prefs.slot_count).sort((a, b) => a - b);

  let prefsTimer = null;
  const savePrefs = () => {
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(() => {
      invoke("save_run_prefs", {
        slot_count: state.prefs.slot_count,
        slots: selectedSlots(),
        access_mode: state.prefs.access_mode,
        close_when_done: state.prefs.close_when_done,
        public_fallback: state.prefs.public_fallback,
      }, { quiet: true });
    }, 300);
  };

  const runConfig = () => ({
    slots: selectedSlots(),
    access_mode: state.prefs.access_mode,
    server_profile: state.prefs.access_mode === "shared" ? "Shared private server" : "My private server",
    server_code: $("#serverCode").value,
    slot_count: state.prefs.slot_count,
    close_when_done: state.prefs.close_when_done,
    public_fallback: state.prefs.public_fallback,
    run_monitor: $("#runMonitor").value,
    mute_roblox: switchOn($("#muteRoblox")),
    low_graphics: switchOn($("#lowGraphics")),
    low_cpu_priority: switchOn($("#lowCpuPriority")),
  });

  // Chips and slot tiles are built once and patched in place; the poll runs every 400 ms during a run.
  const chipButtons = [];
  let chipsKey = "";
  const renderChips = () => {
    if (!chipButtons.length) {
      for (let slot = 1; slot <= 12; slot += 1) {
        chipButtons.push(h("button", {
          type: "button",
          text: String(slot),
          onclick: () => {
            state.prefs.slots.has(slot) ? state.prefs.slots.delete(slot) : state.prefs.slots.add(slot);
            savePrefs();
            renderChips();
            renderDashboard();
          },
        }));
      }
      $("#slotChips").append(...chipButtons);
    }
    const key = `${state.prefs.slot_count}|${selectedSlots().join(",")}|${state.running}`;
    if (key === chipsKey) return;
    chipsKey = key;
    chipButtons.forEach((button, index) => {
      const slot = index + 1;
      const on = state.prefs.slots.has(slot) && slot <= state.prefs.slot_count;
      button.className = on ? "on" : "";
      button.setAttribute("aria-pressed", String(on));
      button.disabled = slot > state.prefs.slot_count || state.running;
    });
    $("#selectionSummary").textContent = `${plural(selectedSlots().length, "slot")} selected in Settings`;
  };

  // ---------- dashboard ----------

  const trackReward = (day) => state.streak?.track?.find((entry) => entry.day === day)?.reward ?? "";

  // The day a slot is on today: what this run read, today's claim, or the day after yesterday's claim.
  // Null when the last claim is too old to say (the backend marks it stale).
  const slotDay = (slot) => {
    const result = state.report?.slots?.find((entry) => entry.slot === slot.slot);
    if (result?.day && claimed(result.status)) return result.day;
    return slot.claimed_today ? slot.streak_day : slot.next_day;
  };

  const selectedDays = () => (state.streak?.slots ?? [])
    .filter((slot) => state.prefs.slots.has(slot.slot))
    .map(slotDay)
    .filter(Boolean);

  // The day most selected slots are on today; each tile shows its own slot's day.
  const todayDay = () => {
    const counts = new Map();
    for (const day of selectedDays()) counts.set(day, (counts.get(day) || 0) + 1);
    let best = null;
    for (const [day, count] of counts) if (!best || count > best[1]) best = [day, count];
    return best?.[0] ?? null;
  };

  // The engine's hold messages are written for the log; players get the reason in plain words.
  const heldReason = (detail = "") => {
    if (/pending/i.test(detail)) return "Last claim wasn't confirmed. Held so it can't claim twice; clears once the game shows it went through";
    if (/maximum claim day/i.test(detail)) return "Day is past your maximum claim day";
    if (/minimum claim day/i.test(detail)) return "Day is below your minimum claim day";
    if (/local history expected/i.test(detail)) return "Day doesn't match its history, so its streak rule held it";
    return detail || "Held";
  };

  // Tiles are small, so they get a short version; the full reason goes in the tooltip.
  const heldShort = (detail = "") => {
    if (/pending/i.test(detail)) return "Unconfirmed claim";
    if (/maximum claim day/i.test(detail)) return "Past max day";
    if (/minimum claim day/i.test(detail)) return "Below min day";
    if (/local history expected/i.test(detail)) return "Day mismatch";
    return "Held";
  };

  const slotView = (slot) => {
    const info = state.streak?.slots?.find((entry) => entry.slot === slot) ?? {};
    const result = state.report?.slots?.find((entry) => entry.slot === slot);
    const selected = state.prefs.slots.has(slot);
    const activeIndex = state.activeSlots.indexOf(slot);
    const currentIndex = state.activeSlots.indexOf(state.currentSlot);
    let kind = "";
    let tag = "Due";
    let text = info.next_reward || trackReward(info.next_day) || (info.stale ? "Checked in run" : "Not claimed yet");
    let title = "";
    let dim = true;
    // Before today's claim: last claimed day → today's day. After it: just the day claimed.
    let foot = info.next_day
      ? (info.streak_day ? `Day ${info.streak_day} → ${info.next_day}` : `Day ${info.next_day}`)
      : info.stale ? `Last seen Day ${info.streak_day}` : "Day ?";

    if (result) {
      if (claimed(result.status)) {
        kind = "ok";
        tag = result.status === "claimed_verified" ? "Verified" : result.status === "claimed_global_locked" ? "Claimed (shared)" : "Claimed";
        text = result.received_reward || result.reward || "Claimed";
        dim = false;
        foot = result.day ? `Day ${result.day}` : "";
      } else if (result.status === "held") {
        kind = "held";
        tag = "Held";
        text = heldShort(result.detail);
        title = heldReason(result.detail);
      } else {
        kind = "bad";
        tag = "Issue";
        text = String(result.detail || result.status || "").replaceAll("_", " ");
      }
    } else if (state.running && slot === state.currentSlot) {
      kind = "live";
      tag = "Claiming";
      text = "In progress…";
      dim = false;
    } else if (state.running && activeIndex >= 0 && activeIndex < currentIndex) {
      kind = "ok";
      tag = "Done";
      text = "Reward at end";
      title = "The reward shows when the run ends";
    } else if (state.running && activeIndex >= 0) {
      tag = "Queued";
    } else if (info.claimed_today) {
      kind = "ok";
      tag = "Claimed";
      text = info.claimed_today;
      dim = false;
      foot = info.streak_day ? `Day ${info.streak_day}` : "";
    } else if (!selected || info.mode === "hold") {
      kind = "held";
      tag = "Held";
      text = selected ? "Held by rule" : "Not selected";
      if (selected) title = "Held by its streak rule";
    }

    return { kind, tag, text, title, dim, foot };
  };

  const tiles = [];
  const renderDashboard = () => {
    if (!tiles.length) {
      for (let slot = 1; slot <= 12; slot += 1) {
        const tile = { tag: h("span"), text: h("strong"), foot: h("small"), key: "" };
        tile.root = h("div", { class: "slot" }, h("div", { class: "slot-top" }, h("b", { text: String(slot) }), tile.tag), tile.text, tile.foot);
        tiles.push(tile);
      }
      $("#slotTiles").append(...tiles.map((tile) => tile.root));
    }
    tiles.forEach((tile, index) => {
      const slot = index + 1;
      tile.root.hidden = slot > state.prefs.slot_count;
      if (tile.root.hidden) return;
      const view = slotView(slot);
      const key = [view.kind, view.tag, view.text, view.title, view.dim, view.foot].join("|");
      if (key === tile.key) return;
      tile.key = key;
      tile.root.className = `slot ${view.kind}`;
      tile.root.title = view.title;
      tile.tag.className = `tag ${view.kind}`;
      tile.tag.textContent = view.tag;
      tile.text.className = view.dim ? "dim" : "";
      tile.text.textContent = view.text;
      tile.foot.textContent = view.foot;
    });
    const day = todayDay();
    $("#todayValue").textContent = day ? `Day ${day}` : "—";
    $("#todayReward").textContent = day ? trackReward(day) : "Read during the next run";
  };

  // The log feed only appends. The backend keeps its last 250 lines, so once it's full the list shifts
  // left instead of growing; the last two lines shown are found again to know where the new ones start.
  const LOG_LINES_SHOWN = 80;
  const logView = { count: 0, tail: [] };
  const seenEnd = (logs) => {
    const [before, last] = logView.tail;
    if (last === undefined) return 0;
    for (let index = Math.min(logs.length, logView.count) - 1; index >= 0; index -= 1) {
      if (logs[index] === last && (before === undefined || logs[index - 1] === before)) return index + 1;
    }
    return -1;
  };

  const renderLogs = () => {
    const logs = state.logs;
    const feed = $("#logFeed");
    const atBottom = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 8;
    let from = seenEnd(logs);
    if (from < 0) {
      // New run or a shorter list: what's on screen no longer matches, start over.
      feed.replaceChildren();
      from = Math.max(0, logs.length - LOG_LINES_SHOWN);
    }
    const fresh = logs.slice(Math.max(from, logs.length - LOG_LINES_SHOWN));
    logView.count = logs.length;
    logView.tail = logs.length >= 2 ? logs.slice(-2) : [undefined, logs[0]];
    if (fresh.length) {
      feed.querySelector(".cur")?.classList.remove("cur");
      feed.querySelector(".placeholder")?.remove();
      feed.append(...fresh.map((line) => h("p", { text: line })));
      while (feed.children.length > LOG_LINES_SHOWN) feed.children[0].remove();
    }
    const lastLine = feed.children[feed.children.length - 1];
    if (!lastLine) feed.append(h("p", { class: "dim placeholder", text: "Waiting to start." }));
    else if (!lastLine.classList.contains("placeholder")) lastLine.classList.toggle("cur", state.running);
    if (atBottom) feed.scrollTop = feed.scrollHeight;
  };

  // ---------- compact pill ----------

  const renderPill = () => {
    const run = runState();
    const statusText = String(state.status).replaceAll("_", " ");
    const results = state.report?.slots ?? [];
    $("#pillText").textContent = run === "running"
      ? (state.currentSlot ? `Slot ${state.currentSlot} · ${statusText}` : statusText)
      : runText(run);
    $("#pillDot").className = `dot pywebview-drag-region ${RUN_DOTS[run]}`;
    $("#pillStop").disabled = !state.running || run === "stopping";
    $("#pillStop").textContent = run === "stopping" ? "Stopping…" : "Stop";
    const slots = state.activeSlots.length ? state.activeSlots : selectedSlots();
    const currentIndex = slots.indexOf(state.currentSlot);
    $("#pillSegs").replaceChildren(...slots.map((slot, index) => {
      const result = results.find((entry) => entry.slot === slot);
      let kind = "";
      if (result) kind = claimed(result.status) ? "ok" : "bad";
      else if (state.running && index < currentIndex) kind = "ok";
      else if (state.running && slot === state.currentSlot) kind = "live";
      return h("i", { class: kind, title: `Slot ${slot}` });
    }));
  };

  const showPill = (compact) => {
    state.compact = compact;
    document.body.classList.toggle("compact", compact);
    $("#pill").hidden = !compact;
    renderPill();
  };

  const setCompact = async (compact) => {
    // Swap to the pill before the window shrinks, so the full layout never flashes squashed into the small window.
    if (compact) showPill(true);
    const result = await invoke("window_action", compact ? "compact" : "expand");
    if (result === null) {
      if (compact) showPill(false);
      return;
    }
    if (!compact) showPill(false);
  };

  // ---------- run state ----------

  // One run state drives the status line, dots, START/STOP and the pill.
  const runState = () => {
    if (state.running) {
      if (state.status === "stopping") return "stopping";
      if (state.status === "paused") return "paused";
      if (state.status === "starting" || state.status === "idle") return "starting";
      return "running";
    }
    if (state.starting) return "starting";
    const finished = { complete: "done", needs_attention: "attention", failed: "failed" }[state.status];
    return finished ?? (state.report ? "done" : "idle");
  };
  const RUN_DOTS = { idle: "idle", starting: "live", running: "live", paused: "warn", stopping: "live", done: "", attention: "warn", failed: "bad" };
  const START_LABELS = { starting: "STARTING…", running: "CLAIMING…", paused: "PAUSED", stopping: "STOPPING…" };

  const runSummary = () => {
    const results = state.report?.slots ?? [];
    const ok = results.filter((slot) => claimed(slot.status)).length;
    const held = results.filter((slot) => slot.status === "held").length;
    const issues = results.length - ok - held;
    return `${ok} claimed${issues ? `, ${plural(issues, "issue")}` : ""}${held ? `, ${held} held` : ""}`;
  };

  const runText = (run) => ({
    idle: "Ready",
    starting: "Starting…",
    paused: "Paused",
    stopping: "Stopping…",
    done: `Done: ${runSummary()}`,
    attention: `Needs attention: ${runSummary()}`,
    failed: `Failed: ${state.runError || "see the activity log"}`,
  })[run] ?? String(state.status).replaceAll("_", " ");

  const renderRunState = () => {
    const run = runState();
    $("#windowFrame").dataset.runState = run;
    const position = state.activeSlots.indexOf(state.currentSlot);
    const statusText = String(state.status).replaceAll("_", " ");
    let nowText = runText(run);
    if (run === "running" || run === "starting") nowText = state.ticker || `${statusText.charAt(0).toUpperCase()}${statusText.slice(1)}…`;
    if (run === "paused") nowText = state.pausedReason || "Paused. Click back into Roblox to continue.";
    $("#nowText").textContent = nowText;
    $("#nowRight").textContent = state.running && position >= 0 ? `${position + 1}/${state.activeSlots.length}` : "";
    for (const id of ["#nowDot", "#sideDot"]) $(id).className = `dot ${RUN_DOTS[run]}`;
    const sideText = run === "running" && position >= 0 ? `Claiming ${position + 1}/${state.activeSlots.length}`
      : run === "running" ? statusText
      : { done: "Done", attention: "Needs attention", failed: "Failed" }[run] ?? runText(run);
    $("#sideStatus").textContent = sideText;
    $("#sideDot").title = sideText;

    $("#startButton").disabled = state.running || state.starting || state.settingsError;
    $("#startButton").textContent = START_LABELS[run] ?? "START";
    $("#stopButton").disabled = !state.running || run === "stopping";
    $("#stopButton").textContent = run === "stopping" ? "STOPPING…" : "STOP";
    if (state.compact) renderPill();
  };

  const renderProgress = () => {
    const bar = $("#progress");
    const width = `${state.progress}%`;
    if (bar.style.width === width) return;
    if (state.progress < (renderProgress.shown ?? 0)) {
      // A new run starts at 0; sliding back from 100% would read as the run going backwards.
      bar.style.transition = "none";
      bar.style.width = width;
      void bar.offsetWidth;
      requestAnimationFrame(() => { bar.style.transition = ""; });
    } else {
      bar.style.width = width;
    }
    renderProgress.shown = state.progress;
  };

  // ---------- state poll ----------

  const applySettings = (settings) => {
    const prefs = settings.run_prefs ?? settings;
    if (prefs.slot_count || settings.configured_slot_count) state.prefs.slot_count = Number(prefs.slot_count || settings.configured_slot_count);
    if (Array.isArray(prefs.slots)) state.prefs.slots = new Set(prefs.slots.map(Number));
    else if (!state.prefs.slots.size) state.prefs.slots = new Set(Array.from({ length: state.prefs.slot_count }, (_, index) => index + 1));
    if (prefs.access_mode) state.prefs.access_mode = prefs.access_mode;
    state.prefs.close_when_done = prefs.close_when_done === true;
    state.prefs.public_fallback = prefs.public_fallback === true;

    $("#slotCount").value = String(state.prefs.slot_count);
    setSegment($("#accessMode"), state.prefs.access_mode);
    $("#sharedCodeRow").hidden = state.prefs.access_mode !== "shared";
    setSwitch($("#closeWhenDone"), state.prefs.close_when_done);
    setSwitch($("#publicFallback"), state.prefs.public_fallback);
    setSwitch($("#muteRoblox"), settings.mute_roblox === true);
    setSwitch($("#lowGraphics"), settings.low_graphics === true);
    setSwitch($("#lowCpuPriority"), settings.low_cpu_priority === true);
    setSwitch($("#notificationsEnabled"), settings.notifications_enabled === true);
    $("#notificationHint").textContent = settings.has_webhook
      ? "A protected webhook is saved. Paste a new link to replace it."
      : "Paste a Discord webhook link. Codes, screenshots and usernames are never sent.";
    Object.entries(settings.sequence_modes ?? {}).forEach(([slot, mode]) => { state.plan[slot] = { ...state.plan[slot], mode }; });
    Object.entries(settings.sequence_align_targets ?? {}).forEach(([slot, target]) => { state.plan[slot] = { ...state.plan[slot], target }; });
    state.settingsApplied = true;
    renderChips();
    renderStreak();
    if (currentView() === "stats") refreshSlotStats();
  };

  const renderMonitors = (monitors = [], selected = "current") => {
    const selector = $("#runMonitor");
    const choices = Array.isArray(monitors) && monitors.length ? monitors : [{ value: "current", label: "Current display" }];
    if (selector.options.length === choices.length && selector.dataset.loaded) return;
    selector.dataset.loaded = "1";
    selector.replaceChildren(...choices.map((monitor) => new Option(
      monitor.label || String(monitor.value), monitor.value || "current", false, (monitor.value || "current") === selected,
    )));
    if (![...selector.options].some((option) => option.value === selected)) selector.value = "current";
  };

  const update = (next) => {
    const wasRunning = state.running;
    state.running = Boolean(next.running);
    state.status = next.status || "idle";
    state.currentSlot = next.current_slot ?? null;
    state.logs = Array.isArray(next.logs) ? next.logs : state.logs;
    state.report = next.report ?? null;
    // While running, live results stand in for the final report.
    if (Array.isArray(next.live_results) && (state.running || (!state.report && next.live_results.length))) state.report = { slots: next.live_results };
    if (Array.isArray(next.history)) {
      const changed = next.history.length !== state.history.length || next.history.at(-1)?.started_utc !== state.history.at(-1)?.started_utc;
      state.history = next.history;
      state.historyLoaded = true;
      if (changed && currentView() === "history") renderRuns();
      if (changed) renderEmbedPreview();
    }
    if (state.historyLoaded) $("#firstRun").hidden = state.history.length > 0;
    if (Array.isArray(next.active_slots)) state.activeSlots = next.active_slots.map(Number);
    state.progress = Math.max(0, Math.min(100, Number(next.progress || 0)));
    state.ticker = next.ticker || "";
    state.pausedReason = next.paused_reason || "";
    state.settingsError = Boolean(next.settings_error);
    if (next.error) state.runError = next.error;

    const slotText = state.currentSlot && state.activeSlots.length ? `${state.currentSlot}|${state.activeSlots.length}` : String(state.currentSlot ?? "");
    if ($("#slotValue").dataset.key !== slotText) {
      $("#slotValue").dataset.key = slotText;
      $("#slotValue").replaceChildren(document.createTextNode(state.currentSlot ? `${state.currentSlot} ` : "—"));
      if (state.currentSlot && state.activeSlots.length) $("#slotValue").append(h("em", { text: `of ${state.activeSlots.length}` }));
    }
    $("#elapsedValue").textContent = next.elapsed || "00:00";
    $("#pillTime").textContent = next.elapsed || "00:00";
    if (typeof next.hotkey === "string" && !state.hotkeySaving) state.hotkey = next.hotkey;
    renderHotkey();
    renderRunState();
    renderProgress();

    renderLogs();
    if (next.error && next.error !== state.lastError) {
      state.lastError = next.error;
      toast(next.error, true);
      invoke("consume_last_error", undefined, { quiet: true });
    }
    if (!next.error) state.lastError = "";
    $("#settingsWarning").hidden = !next.settings_error;
    $("#settingsWarningText").textContent = next.settings_error || "";

    if (next.close_pending) state.closeRequested = true;
    if (state.closeRequested && !state.running) {
      state.closeRequested = false;
      invoke("window_action", "close");
      return;
    }
    if (next.settings && !state.settingsApplied) applySettings(next.settings);
    if (next.monitors) renderMonitors(next.monitors, next.settings?.run_monitor);
    if (wasRunning && !state.running) {
      // A run just finished: refresh everything that reads the history.
      loadStreak();
      if (currentView() === "stats") loadStats();
      if (currentView() === "history") renderRuns();
      renderEmbedPreview();
    }
    renderDashboard();
    renderChips();
    // The global hotkey asks for a start through the poll; take the same path as START and F6.
    if (next.hotkey_start_requested && !state.running && !state.hotkeyCapturing && !state.hotkeySaving) $("#startButton").click();
  };

  const poll = async () => {
    if (state.pollBusy || state.unloaded) return;
    clearTimeout(state.poll);
    state.poll = null;
    state.pollBusy = true;
    const revision = state.pollRevision;
    try {
      const next = await invoke("get_state", undefined, { quiet: true });
      if (next && !state.unloaded && revision === state.pollRevision) update(next);
    } finally {
      state.pollBusy = false;
      if (!state.unloaded) state.poll = setTimeout(poll, state.running ? 400 : 1500);
    }
  };

  // ---------- streak ----------

  const loadStreak = async () => {
    const streak = await invoke("get_streak", undefined, { quiet: true });
    if (!streak) return;
    state.streak = streak;
    renderStreak();
    renderDashboard();
  };

  const renderStreak = () => {
    const streak = state.streak;
    if (!streak) return;
    const today = todayDay();
    const slotsOnDay = new Map();
    for (const slot of streak.slots ?? []) {
      const day = slotDay(slot);
      if (day) slotsOnDay.set(day, (slotsOnDay.get(day) || 0) + 1);
    }
    $("#track").replaceChildren(...(streak.track ?? []).map(({ day, reward }) => {
      const classes = ["day"];
      if (today && day < today) classes.push("done");
      if (day === today) classes.push("cur");
      if (today && day === today + 1) classes.push("next");
      if (day === 14) classes.push("gold");
      const label = day === today ? plural(slotsOnDay.get(day) || 0, "slot") : today && day === today + 1 ? "tomorrow" : slotsOnDay.get(day) ? plural(slotsOnDay.get(day), "slot") : "";
      return h("div", { class: classes.join(" ") }, h("b", { text: String(day) }), h("span", { text: reward }), label ? h("em", { text: label }) : null);
    }));

    const head = h("div", { class: "tr th" }, ...["Slot", "Claimed today", "Streak", "Next reward", "Rule"].map((text) => h("span", { text })));
    const rows = (streak.slots ?? []).map((slot) => {
      const plan = state.plan[slot.slot] ?? {};
      const mode = plan.mode ?? slot.mode ?? "always";
      const pips = h("i", { class: "pips" });
      pips.style.setProperty("--n", String(slot.streak_day || 0));
      const modeSelect = h("select", {
        onchange: (event) => {
          const mainBefore = mainClaimSlot();
          const target = Number(plan.target ?? slot.align_target ?? (slot.slot === 1 ? 2 : 1));
          state.plan[slot.slot] = { ...state.plan[slot.slot], mode: event.target.value, target };
          followMainClaimSlot(mainBefore, slot.slot);
          renderStreak();
          queuePlanSave();
        },
      }, ...[["Claim", "always"], ...(state.prefs.slot_count > 1 ? [["Align", "align"]] : []), ["Hold", "hold"]].map(([label, value]) => new Option(label, value, false, value === mode)));
      const targetSelect = h("select", {
        title: `Align slot ${slot.slot} with another slot`,
        onchange: (event) => {
          state.plan[slot.slot] = { ...state.plan[slot.slot], target: Number(event.target.value) };
          queuePlanSave();
        },
      }, ...Array.from({ length: state.prefs.slot_count }, (_, index) => index + 1)
        .filter((target) => target !== slot.slot)
        .map((target) => new Option(`with slot ${target}`, String(target), false, target === Number(plan.target ?? slot.align_target ?? (slot.slot === 1 ? 2 : 1)))));
      return h("div", { class: "tr" },
        h("span", { text: String(slot.slot) }),
        h("span", { class: slot.claimed_today ? "good" : "dim", text: slot.claimed_today || (mode === "hold" ? "Held" : "Not yet") }),
        h("span", {}, pips, h("b", { class: "mono", text: slot.streak_day ? String(slot.streak_day) : "—" })),
        h("span", { text: slot.next_reward || "—" }),
        h("span", { class: "rule" }, modeSelect, mode === "align" ? targetSelect : null),
      );
    });
    $("#streakTable").replaceChildren(head, ...rows);
  };

  // The first slot set to Claim is the "main" slot Align follows by default.
  const mainClaimSlot = () => {
    for (let slot = 1; slot <= state.prefs.slot_count; slot += 1) {
      if ((state.plan[slot]?.mode ?? "always") === "always") return slot;
    }
    return null;
  };

  // When the main Claim slot moves, Align slots that followed the old one move with it,
  // and a slot just switched to Align starts by following it. Picks of other targets stay.
  const followMainClaimSlot = (mainBefore, changedSlot) => {
    const main = mainClaimSlot();
    if (!main) return;
    for (let slot = 1; slot <= state.prefs.slot_count; slot += 1) {
      const plan = state.plan[slot];
      if (plan?.mode !== "align" || slot === main) continue;
      if (slot === changedSlot || Number(plan.target) === mainBefore) state.plan[slot] = { ...plan, target: main };
    }
  };

  const savePlan = async () => {
    const modes = {};
    const targets = {};
    for (let slot = 1; slot <= state.prefs.slot_count; slot += 1) {
      modes[slot] = state.plan[slot]?.mode ?? "always";
      if (state.plan[slot]?.target != null || slot !== 1 || modes[slot] === "align") {
        targets[slot] = Number(state.plan[slot]?.target ?? (slot === 1 ? 2 : 1));
      }
    }
    await invoke("save_plan", {
      sequence_modes: modes,
      sequence_align_targets: targets,
      run_monitor: $("#runMonitor").value || "current",
      mute_roblox: switchOn($("#muteRoblox")),
      low_graphics: switchOn($("#lowGraphics")),
      low_cpu_priority: switchOn($("#lowCpuPriority")),
    });
  };

  // Streak rules save on change, debounced like the run prefs, so there's nothing to forget to save.
  let planTimer = null;
  const queuePlanSave = () => {
    clearTimeout(planTimer);
    planTimer = setTimeout(() => savePlan(), 300);
  };

  // ---------- history ----------

  const runLabel = (run) => {
    const when = new Date(run.started_utc || run.finished_utc || Date.now());
    const today = new Date();
    const sameDay = when.toDateString() === today.toDateString();
    const time = when.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return sameDay ? `Today, ${time}` : `${when.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}, ${time}`;
  };
  const runDay = (run) => {
    const days = (run.slots ?? []).map((slot) => slot.day).filter(Boolean);
    return days.length ? Math.min(...days) : null;
  };

  const runDate = (run) => new Date(run.started_utc || run.finished_utc || Date.now());
  const dateLabel = (when) => when.toDateString() === new Date().toDateString()
    ? "Today"
    : when.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  const claimedCount = (run) => (run.slots ?? []).filter((slot) => claimed(slot.status)).length;

  const runRow = (run) => {
    const slots = run.slots ?? [];
    const ok = claimedCount(run);
    const day = runDay(run);
    const time = runDate(run).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return h("button", {
      type: "button",
      class: `run sub ${state.selectedRun === run.started_utc ? "sel" : ""}`,
      "data-run": run.started_utc,
      onclick: () => selectRun(run.started_utc),
    },
    h("div", {}, h("b", { text: time }), h("small", { text: `${day ? `Day ${day} · ` : ""}${minutes(run.total_seconds)}` })),
    h("span", { class: `tag ${ok === slots.length && !(run.errors ?? []).length ? "ok" : "warn"}`, text: `${ok}/${slots.length}` }));
  };

  // One row per day with the day's total; runs drop down under it. The total isn't
  // "out of N slots" because people can run several accounts on the same day.
  const renderRuns = () => {
    const runs = state.history.slice().reverse();
    if (!runs.length) {
      $("#runList").replaceChildren(h("p", { class: "empty", text: "No runs yet. Your first run will show up here." }));
      return;
    }
    const days = new Map();
    for (const run of runs) {
      const key = runDate(run).toDateString();
      if (!days.has(key)) days.set(key, []);
      days.get(key).push(run);
    }
    state.openDays ??= new Set([days.keys().next().value]);
    $("#runList").replaceChildren(...[...days].flatMap(([key, dayRuns]) => {
      const open = state.openDays.has(key);
      const total = dayRuns.reduce((sum, run) => sum + claimedCount(run), 0);
      const header = h("button", {
        type: "button",
        class: `run run-day ${open ? "open" : ""}`,
        "aria-expanded": String(open),
        onclick: () => {
          if (open) state.openDays.delete(key);
          else state.openDays.add(key);
          renderRuns();
        },
      },
      h("div", {}, h("b", { text: dateLabel(runDate(dayRuns[0])) }), h("small", { text: plural(dayRuns.length, "run") })),
      h("span", { class: `tag ${total ? "ok" : ""}`, text: `${total} claimed` }));
      return open ? [header, ...dayRuns.map(runRow)] : [header];
    }));
    if (!state.selectedRun) selectRun(runs[0].started_utc);
  };

  const selectRun = async (startedUtc) => {
    state.selectedRun = startedUtc;
    state.selectedSlot = null;
    document.querySelectorAll("#runList .run.sub").forEach((row) => {
      row.classList.toggle("sel", row.dataset.run === startedUtc);
    });
    const run = await invoke("get_run", startedUtc, { quiet: true }) ?? state.history.find((entry) => entry.started_utc === startedUtc);
    if (!run || state.selectedRun !== startedUtc) return;
    const day = runDay(run);
    const errors = (run.errors ?? []).length;
    $("#detailTitle").textContent = runLabel(run);
    $("#detailSub").textContent = `${day ? `Day ${day} · ` : ""}${plural(errors, "error")} · ${minutes(run.total_seconds)}`;
    const rows = (run.slots ?? []).map((slot) => {
      const verified = slot.status === "claimed_verified" || slot.status === "claimed_global_locked";
      const dotClass = verified ? "" : claimed(slot.status) ? "warn" : slot.status === "held" ? "off" : "bad";
      const text = slot.received_reward
        || (claimed(slot.status) ? `${slot.reward || "Claimed"} (popup not read)`
          : slot.status === "held" ? `Held: ${heldReason(slot.detail)}`
          : String(slot.status || "").replaceAll("_", " "));
      return h("button", {
        type: "button",
        class: "rx-row",
        "data-slot": String(slot.slot),
        onclick: () => selectReceipt(run.started_utc, slot),
      }, h("span", { class: "mono", text: String(slot.slot) }), h("span", { text }), h("i", { class: `chk ${dotClass}` }));
    });
    $("#rxList").replaceChildren(...rows);
    const first = (run.slots ?? []).find((slot) => slot.has_receipt) ?? (run.slots ?? [])[0];
    if (first) selectReceipt(run.started_utc, first);
    else showReceipt(null, "");
  };

  const showReceipt = (dataUri, caption) => {
    $("#receiptImage").hidden = !dataUri;
    if (dataUri) $("#receiptImage").src = dataUri;
    $("#receiptEmpty").hidden = Boolean(dataUri);
    $("#receiptEmpty").textContent = caption && !dataUri ? "No reward popup was saved for this slot." : "Pick a slot to see its reward popup.";
    $("#receiptCaption").textContent = caption;
  };

  const selectReceipt = async (startedUtc, slot) => {
    state.selectedSlot = slot.slot;
    document.querySelectorAll("#rxList .rx-row").forEach((row) => row.classList.toggle("sel", row.dataset.slot === String(slot.slot)));
    const receipt = slot.has_receipt === false || !api()?.get_receipt
      ? null
      : await Promise.resolve().then(() => api().get_receipt(startedUtc, slot.slot)).catch(() => null);
    if (state.selectedRun !== startedUtc || state.selectedSlot !== slot.slot) return;
    showReceipt(receipt?.data_uri ?? null, `Slot ${slot.slot} receipt`);
  };

  // ---------- statistics ----------

  const stat = (label, value, unit, sub, className = "") => h("div", { class: "stat" },
    h("span", { text: label }),
    h("strong", { class: `mono ${className}` }, value, unit ? h("em", { text: ` ${unit}` }) : null),
    h("small", { text: sub }));

  const barChart = (days, valueOf, max, kind) => days.map((entry) => {
    const value = valueOf(entry);
    const bar = h("i");
    bar.style.height = `${value ? Math.max(4, Math.round((value / max) * 100)) : 3}%`;
    const date = new Date(`${entry.date}T12:00:00`);
    return h("div", {
      class: `col ${kind} ${value ? "" : "miss"}`,
      title: `${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}: ${value ? (kind === "dur" ? `${value.toFixed(1)} min` : plural(value, "claim")) : "no run"}`,
    }, bar, h("span", { class: "mono", text: String(date.getDate()) }));
  });

  const meterRow = (label, value, fraction) => {
    const meter = h("i", { class: "meter" });
    meter.style.setProperty("--w", `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`);
    return h("div", { class: "kv" }, h("span", { text: label }), meter, h("b", { class: "mono", text: value }));
  };

  const duration = (totalMinutes) => {
    const hours = Math.floor(totalMinutes / 60);
    const rest = Math.round(totalMinutes % 60);
    return hours ? `${hours} h ${rest} min` : `${rest} min`;
  };

  const renderCollected = (selector, collected = {}) => {
    const rows = [
      ["Zeni", number(collected.zeni), collected.zeni],
      ["Mastery Points", `+${number(collected.mastery_points)}`, collected.mastery_points],
      ["2× XP time", duration(collected.xp_boost_minutes || 0), collected.xp_boost_minutes],
      ["2× Zeni time", duration(collected.zeni_boost_minutes || 0), collected.zeni_boost_minutes],
      ["2× Drops time", duration(collected.drop_boost_minutes || 0), collected.drop_boost_minutes],
      ["Dragon Balls", `${number(collected.dragon_balls)} (${collected.dragon_balls_unique || 0} unique)`, collected.dragon_balls],
      ["Consumables", `${number(collected.consumables)} items`, collected.consumables],
      ["Add-On Packs", number(collected.addon_packs), collected.addon_packs],
    ];
    // Meters are relative within each row type, so show them as "has any" against the busiest row.
    const busiest = Math.max(1, ...rows.map(([, , raw]) => Math.log10(1 + (raw || 0))));
    $(selector).replaceChildren(...rows.map(([label, value, raw]) => meterRow(label, value, Math.log10(1 + (raw || 0)) / busiest)));
  };

  const loadSlotStats = async (slot = state.statsSlot) => {
    state.statsSlot = slot;
    const request = ++state.slotStatsRequest;
    $("#exportSlotCsv").disabled = true;
    $("#slotDetailsBody").hidden = true;
    $("#slotDetailsBody").setAttribute("aria-busy", "true");
    const stats = await invoke("get_slot_stats", slot, { quiet: true });
    // Switching slots can finish out of order, including a failed older request.
    if (request !== state.slotStatsRequest || state.unloaded) return;
    $("#perSlotSection").hidden = !stats;
    $("#slotDetailsBody").setAttribute("aria-busy", "false");
    if (!stats) return;
    $("#slotDetailsTitle").textContent = `Slot ${slot} details`;
    $("#slotDetailsBody").hidden = false;
    $("#exportSlotCsv").disabled = false;
    $("#slotDetailTotals").replaceChildren(
      stat("CLAIMS", number(stats.claims), "", ""),
      stat("VERIFIED", number(stats.verified), "", `${Number(stats.verified_pct || 0).toFixed(1)}% of claims`, "good"),
      stat("CURRENT DAY", stats.current_day ? String(stats.current_day) : "—", "", ""),
      stat("LONGEST STREAK", number(stats.longest_streak), "days", ""),
      stat("AVG CLAIM", stats.avg_seconds == null ? "—" : Number(stats.avg_seconds).toFixed(1), stats.avg_seconds == null ? "" : "s", ""),
    );
    $("#slotLastClaim").textContent = stats.last_claimed_utc
      ? `Last claimed: ${new Date(stats.last_claimed_utc).toLocaleString("en-GB")}` : "No claims recorded for this slot.";
    renderCollected("#slotCollected", stats.collected);
    const balls = stats.dragon_balls_detail ?? [];
    $("#slotDragonBalls").textContent = balls.length
      ? balls.map(([name, count]) => `${name}${count > 1 ? ` x${count}` : ""}`).join(", ")
      : "No Dragon Balls read yet.";
    const items = stats.items ?? [];
    $("#slotItems").replaceChildren(...(items.length
      ? items.slice(0, 8).map(([name, count]) => meterRow(name, number(count), 0))
      : [h("p", { class: "empty", text: "No item rewards read yet." })]),
    ...(items.length > 8 ? [h("p", { class: "empty", text: `${items.length - 8} more item types in the CSV export.` })] : []));
    const recent = stats.recent ?? [];
    $("#slotRecent").replaceChildren(recent.length
      ? h("table", { "aria-label": `Recent claims for slot ${slot}` },
        h("thead", {}, h("tr", {}, ...["Date", "Day", "Status", "Reward", "Time"].map((text) => h("th", { scope: "col", text })))),
        h("tbody", {}, ...recent.map((claim) => h("tr", {},
          ...[claim.date, claim.day ?? "—", String(claim.status || "").replaceAll("_", " "), claim.received_reward || "—",
            claim.seconds == null ? "—" : `${Number(claim.seconds).toFixed(1)} s`].map((text) => h("td", { text }))))))
      : h("p", { class: "empty", text: "No recent claims." }));
  };

  const refreshSlotStats = () => {
    const count = state.prefs.slot_count;
    state.statsSlot = Math.min(state.statsSlot, count);
    $("#statsSlotPicker").replaceChildren(...Array.from({ length: count }, (_, index) => new Option(`Slot ${index + 1}`, String(index + 1))));
    $("#statsSlotPicker").value = String(state.statsSlot);
    return loadSlotStats();
  };

  const loadStats = async () => {
    refreshSlotStats();
    const stats = await invoke("get_stats", undefined, { quiet: true });
    if (!stats) return;
    const totals = stats.totals ?? {};
    const streakSlots = totals.longest_streak_slots ?? [];
    $("#statTotals").replaceChildren(
      stat("TOTAL CLAIMS", number(totals.claims), "", `across ${plural(totals.runs || 0, "run")}`),
      stat("VERIFIED", totals.claims ? `${Number(totals.verified_pct || 0).toFixed(1)}%` : "—", "", `${plural(totals.unread || 0, "unread popup")}`, "good"),
      stat("AVG RUN", totals.avg_run_seconds ? (totals.avg_run_seconds / 60).toFixed(1) : "—", totals.avg_run_seconds ? "min" : "",
        totals.avg_slot_seconds ? `${Math.floor(totals.avg_slot_seconds / 60)} min ${Math.round(totals.avg_slot_seconds % 60)} s per slot` : ""),
      stat("LONGEST STREAK", String(totals.longest_streak_days || 0), "days",
        streakSlots.length ? `slot${streakSlots.length > 1 ? "s" : ""} ${streakSlots.length > 3 ? `${streakSlots[0]} to ${streakSlots.at(-1)}` : streakSlots.join(", ")}` : ""),
    );

    const days = stats.days ?? [];
    const maxClaims = Math.max(1, ...days.map((entry) => entry.claims || 0));
    const maxMinutes = Math.max(1, ...days.map((entry) => (entry.run_seconds || 0) / 60));
    $("#claimsChart").replaceChildren(...barChart(days, (entry) => entry.claims || 0, maxClaims, ""));
    $("#durationChart").replaceChildren(...barChart(days, (entry) => (entry.run_seconds || 0) / 60, maxMinutes, "dur"));
    renderCollected("#collected", stats.collected);

    const balls = stats.dragon_balls_detail ?? [];
    $("#slotDragonBalls").textContent = balls.length
      ? balls.map(([name, count]) => `${name}${count > 1 ? ` x${count}` : ""}`).join(", ")
      : "No Dragon Balls read yet.";
    const items = stats.items ?? [];
    const topCount = Math.max(1, ...items.map((item) => item.count));
    $("#topItems").replaceChildren(...(items.length
      ? items.map((item) => meterRow(item.name, number(item.count), item.count / topCount))
      : [h("p", { class: "empty", text: "No item rewards read yet." })]));

    const head = h("div", { class: "tr th" }, ...["Slot", "Claims", "Missed", "Verified", "Zeni earned", "Streak"].map((text) => h("span", { text })));
    $("#slotStats").replaceChildren(head, ...(stats.slots ?? []).map((slot) => {
      const pips = h("i", { class: "pips" });
      pips.style.setProperty("--n", String(slot.streak_day || 0));
      return h("div", { class: "tr" },
        h("span", { text: String(slot.slot) }),
        h("span", { class: "mono", text: number(slot.claims) }),
        h("span", { class: "mono dim", text: number(slot.missed) }),
        h("span", { class: "mono good", text: slot.claims ? `${Math.round(slot.verified_pct || 0)}%` : "—" }),
        h("span", { class: "mono", text: number(slot.zeni) }),
        h("span", {}, pips, h("b", { class: "mono", text: slot.streak_day ? String(slot.streak_day) : "—" })));
    }));
  };

  // ---------- schedule + discord ----------

  const scheduleForm = () => ({
    enabled: switchOn($("#schedEnabled")),
    offset_minutes: Number($("#schedOffset").value),
    catch_up: switchOn($("#schedCatchUp")),
    day1: $("#schedDay1").dataset.value || "hold",
    roblox_busy: $("#schedBusy").value,
  });

  const renderSchedule = (schedule) => {
    if (!schedule) return;
    state.schedule = schedule;
    setSwitch($("#schedEnabled"), schedule.enabled);
    $("#schedOffset").value = String(schedule.offset_minutes ?? 30);
    setSwitch($("#schedCatchUp"), schedule.catch_up !== false);
    setSegment($("#schedDay1"), schedule.day1 || "hold");
    $("#schedBusy").value = schedule.roblox_busy || "wait";
    if (schedule.reset_local) $("#resetHint").textContent = `The daily reset is at ${schedule.reset_local} your time.`;
    const next = schedule.next_run_local ? new Date(schedule.next_run_local) : null;
    $("#schedStatus").textContent = schedule.enabled
      ? schedule.task_present
        ? `Next automatic run: ${next ? next.toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "scheduled"}.`
        : "Automatic runs are on, but the Windows task is missing. Toggle it off and on to recreate it."
      : "Automatic runs are off. XynClaim only runs when you press Start.";
    updateResetClock();
  };

  const saveSchedule = async () => {
    const saved = await invoke("save_schedule", scheduleForm());
    if (saved) renderSchedule(saved);
    else if (state.schedule) renderSchedule(state.schedule);
  };

  const renderEmbedPreview = () => {
    const last = state.history.at(-1);
    const slots = last?.slots ?? [];
    const lines = slots.filter((slot) => claimed(slot.status)).slice(0, 3).map((slot) =>
      h("span", {}, h("code", { text: String(slot.slot).padStart(2, " ") }), ` Day ${slot.day ?? "?"}: ${slot.received_reward || slot.reward || "claimed"}`, h("br")));
    const extra = Math.max(0, slots.filter((slot) => claimed(slot.status)).length - 3);
    const ok = slots.filter((slot) => claimed(slot.status)).map((slot) => slot.slot);
    const nextDay = Math.max(0, ...slots.map((slot) => slot.next_day || 0));
    $("#embedPreview").replaceChildren(
      h("b", { text: "DBOG daily report" }),
      h("p", { class: "mono" }, ...(lines.length ? lines : [h("span", { class: "dim", text: "Your first run's rewards will show here." })]), extra ? h("span", { class: "dim", text: `… ${extra} more` }) : null),
      h("div", { class: "fields" },
        h("div", {}, h("i", { text: "Claimed" }), ok.length ? ok.join(", ") : "—"),
        h("div", {}, h("i", { text: "Next" }), nextDay ? `Day ${nextDay}` : "—"),
        h("div", {}, h("i", { text: "Errors" }), String((last?.errors ?? []).length)),
        h("div", {}, h("i", { text: "Duration" }), last ? minutes(last.total_seconds) : "—")),
    );
  };

  let notificationSaveBusy = false;
  const saveNotifications = async (automatic = false) => {
    if (notificationSaveBusy) return;
    notificationSaveBusy = true;
    const webhookUrl = $("#webhookUrl").value.trim();
    try {
      const result = await invoke("save_notifications", { enabled: switchOn($("#notificationsEnabled")), webhook_url: webhookUrl });
      if (result !== null) {
        setSwitch($("#notificationsEnabled"), result.enabled);
        $("#notificationHint").textContent = result.has_webhook
          ? "A protected webhook is saved. Paste a new link to replace it."
          : "Paste a Discord webhook link. Codes, screenshots and usernames are never sent.";
        $("#webhookUrl").value = "";
        if (!automatic) toast("Discord report settings saved");
      }
    } finally {
      notificationSaveBusy = false;
    }
  };

  // ---------- settings ----------

  const renderHotkey = () => {
    $("#hotkeyLabel").textContent = state.hotkey || "F6";
    $("#hotkeyText").textContent = state.hotkey ? "Start / Stop, works in game" : "Start / Stop while this window is focused";
    $("#hotkeySetting").hidden = state.hotkeyUnavailable || !api()?.save_hotkey;
    $("#hotkeyButton").textContent = state.hotkeyCapturing ? "Press a key..." : state.hotkey || "Off";
    $("#hotkeyButton").setAttribute("aria-pressed", String(state.hotkeyCapturing));
    $("#hotkeyButton").disabled = state.hotkeySaving;
  };

  const cancelHotkeyCapture = () => {
    state.hotkeyCapturing = false;
    renderHotkey();
  };

  const captureHotkey = async (event) => {
    event.preventDefault();
    if (event.repeat || event.isComposing) return;
    if (event.key === "Escape") return cancelHotkeyCapture();
    if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
    const off = ["Backspace", "Delete"].includes(event.key);
    const modifiers = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift"].filter(Boolean);
    const key = /^(Key[A-Z]|Digit[0-9])$/.test(event.code || "") ? event.code.replace(/^(Key|Digit)/, "") : event.key.toUpperCase();
    if (!off && (event.metaKey || !(/^F([1-9]|1[0-2])$/.test(key) || (/^[A-Z0-9]$/.test(key) && modifiers.length)))) {
      toast("Use F1–F12, or a letter or number with Ctrl, Alt or Shift.", true);
      return;
    }
    state.hotkeyCapturing = false;
    state.hotkeySaving = true;
    renderHotkey();
    const saved = await invoke("save_hotkey", off ? "" : [...modifiers, key].join("+"));
    state.hotkeySaving = false;
    if (typeof saved?.hotkey === "string") {
      state.hotkey = saved.hotkey;
      // Discard state reads begun before the new key was saved.
      state.pollRevision += 1;
      toast(state.hotkey ? `Start / Stop key saved: ${state.hotkey}` : "Global Start / Stop key turned off");
    } else {
      state.hotkeyUnavailable = true;
    }
    renderHotkey();
  };

  const loadChecks = async () => {
    const checks = await invoke("setup_checks", undefined, { quiet: true });
    if (!checks) return;
    $("#setupChecks").replaceChildren(...checks.map((check) => h("div", { class: `check ${check.state}` },
      h("i"), h("div", {}, h("b", { text: check.title }), h("small", { text: check.detail })))));
    // Problems that will stop a run are repeated on the dashboard, next to START.
    const bad = checks.filter((check) => check.state === "bad");
    const reason = bad.map((check) => check.detail.replace(/\.$/, "")).join(". ");
    $("#setupBanner").hidden = !bad.length;
    $("#setupBannerText").textContent = bad.length ? `Setup problem: ${reason}.` : "";
    $("#startButton").title = reason;
  };

  // ---------- title bar reset clock ----------

  const updateResetClock = () => {
    const stamp = state.schedule?.next_reset_utc;
    const next = stamp ? new Date(stamp) : null;
    if (!next || Number.isNaN(next.getTime())) {
      $("#resetClock").textContent = "";
      $("#nextResetValue").textContent = "—";
      $("#nextResetSub").textContent = "";
      return;
    }
    const now = new Date();
    if (next <= now) {
      $("#resetClock").textContent = "";
      $("#nextResetValue").textContent = "—";
      $("#nextResetSub").textContent = "";
      if (!state.scheduleRefreshBusy && !state.unloaded) {
        state.scheduleRefreshBusy = true;
        invoke("get_schedule", undefined, { quiet: true }).then((schedule) => {
          if (schedule && !state.unloaded) renderSchedule(schedule);
        }).finally(() => { state.scheduleRefreshBusy = false; });
      }
      return;
    }
    const reset = next.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    const left = Math.round((next - now) / 60000);
    const countdown = `${Math.floor(left / 60)}h ${pad2(left % 60)}m`;
    $("#resetClock").textContent = `Daily reset ${reset} · in ${countdown}`;
    $("#nextResetValue").textContent = countdown;
    $("#nextResetSub").textContent = `at ${reset}`;
  };

  // ---------- navigation ----------

  const currentView = () => (location.hash || "#dashboard").slice(1).split("&")[0];
  const showView = () => {
    let id = currentView();
    if (!document.getElementById(`view-${id}`)) id = "dashboard";
    document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${id}`));
    document.querySelector("main").scrollTop = 0;
    document.querySelectorAll("nav a").forEach((link) => link.toggleAttribute("aria-current", link.dataset.view === id));
    if (id === "stats") loadStats();
    if (id === "history") renderRuns();
    if (id === "settings") loadChecks();
    if (id === "streak") loadStreak();
    if (id === "schedule") renderEmbedPreview();
  };
  addEventListener("hashchange", showView);
  addEventListener("hashchange", cancelHotkeyCapture);
  window.addEventListener("blur", cancelHotkeyCapture);

  // ---------- wiring ----------

  // The title bar and pill drag the window from anywhere; pywebview starts a drag on
  // mousedown inside them, so their buttons stop it from reaching that handler.
  document.querySelectorAll(".tb-icons, .pill button").forEach((control) => {
    control.addEventListener("mousedown", (event) => event.stopPropagation());
  });

  document.querySelectorAll("[data-window]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.window;
      if (action === "compact" || action === "expand") return setCompact(action === "compact");
      const result = await invoke("window_action", action);
      if (result?.action === "stopping-before-close") {
        state.closeRequested = true;
        toast("Stopping safely before closing. XynClaim closes once Roblox is back to normal.");
      }
    });
  });
  $("#resetClock").addEventListener("dblclick", () => invoke("window_action", "toggle-maximize"));

  $("#startButton").addEventListener("click", async () => {
    if (state.starting || state.running || state.closeRequested || state.settingsError) return;
    state.starting = true;
    $("#startButton").disabled = true;
    try {
      const run = await invoke("start_run", runConfig());
      if (!run?.started || state.unloaded) return;
      $("#serverCode").value = "";
      state.pollRevision += 1;
      state.running = true;
      state.status = "starting";
      state.currentSlot = null;
      state.logs = [];
      state.activeSlots = run.slots ?? [];
      state.report = null;
      state.runError = "";
      state.progress = 0;
      state.ticker = "";
      const held = run.held_slots ?? [];
      toast(`Run started for ${plural(state.activeSlots.length, "slot")}${held.length ? ` · held: ${held.join(", ")}` : ""}`);
      poll();
    } finally {
      state.starting = false;
      renderRunState();
      renderProgress();
    }
  });
  const stop = async () => {
    if (await invoke("stop_run") === null) return;
    // Show it straight away; the next poll confirms it.
    if (state.running) state.status = "stopping";
    renderRunState();
    toast("Stopping after the current step");
  };
  $("#stopButton").addEventListener("click", stop);
  $("#pillStop").addEventListener("click", stop);
  // Clears the view only; the cursor stays, so just new lines show up afterwards.
  $("#clearLog").addEventListener("click", () => $("#logFeed").replaceChildren(h("p", { class: "dim placeholder", text: "Waiting to start." })));
  $("#toast").addEventListener("click", () => {
    if (!$("#toast").classList.contains("error")) return;
    clearTimeout(toast.timer);
    $("#toast").classList.remove("show");
  });

  $("#sequencePreset").addEventListener("click", () => {
    for (let slot = 1; slot <= state.prefs.slot_count; slot += 1) state.plan[slot] = { mode: slot === 1 ? "always" : "align", ...(slot > 1 ? { target: 1 } : {}) };
    renderStreak();
    queuePlanSave();
    toast("Slot 1 claims daily; the other slots align with slot 1.");
  });

  $("#openReports").addEventListener("click", () => invoke("open_reports_folder"));
  for (const id of ["#sideDiscord", "#settingsDiscord"]) $(id).addEventListener("click", () => invoke("open_discord"));
  $("#exportCsv").addEventListener("click", async () => {
    const result = await invoke("export_stats_csv");
    if (result?.path) toast(`Saved ${result.path}`);
  });
  $("#statsSlotPicker").addEventListener("change", () => {
    $("#slotDetails").open = true;
    return loadSlotStats(Number($("#statsSlotPicker").value));
  });
  $("#exportSlotCsv").addEventListener("click", async () => {
    const result = await invoke("export_stats_csv", state.statsSlot);
    if (result?.path) toast(`Saved ${result.path.split(/[\\/]/).at(-1)}`);
  });
  $("#hotkeyButton").addEventListener("click", () => {
    state.hotkeyCapturing = !state.hotkeyCapturing;
    renderHotkey();
  });

  bindSwitch("#schedEnabled", saveSchedule);
  bindSwitch("#schedCatchUp", saveSchedule);
  bindSegment("#schedDay1", saveSchedule);
  $("#schedOffset").addEventListener("change", saveSchedule);
  $("#schedBusy").addEventListener("change", saveSchedule);

  bindSwitch("#notificationsEnabled", () => saveNotifications(false));
  $("#webhookUrl").addEventListener("paste", () => window.setTimeout(() => saveNotifications(true), 0));
  $("#saveNotifications").addEventListener("click", () => saveNotifications(false));
  $("#testNotification").addEventListener("click", async () => {
    if (await invoke("test_notification", { webhook_url: $("#webhookUrl").value }) !== null) toast("Test report sent to Discord");
  });

  bindSegment("#accessMode", (value) => {
    state.prefs.access_mode = value;
    $("#sharedCodeRow").hidden = value !== "shared";
    savePrefs();
  });
  bindSwitch("#publicFallback", (on) => { state.prefs.public_fallback = on; savePrefs(); });
  bindSwitch("#closeWhenDone", (on) => { state.prefs.close_when_done = on; savePrefs(); });
  for (const id of ["#muteRoblox", "#lowGraphics", "#lowCpuPriority"]) bindSwitch(id, () => savePlan());
  $("#runMonitor").addEventListener("change", () => savePlan());

  $("#slotCount").replaceChildren(...Array.from({ length: 12 }, (_, index) => new Option(String(index + 1), String(index + 1), index === 11, index === 11)));
  $("#slotCount").addEventListener("change", () => {
    state.prefs.slot_count = Number($("#slotCount").value);
    savePrefs();
    renderChips();
    renderDashboard();
    if (currentView() === "stats") refreshSlotStats();
  });
  $("#selectAllSlots").addEventListener("click", () => {
    state.prefs.slots = new Set(Array.from({ length: state.prefs.slot_count }, (_, index) => index + 1));
    savePrefs();
    renderChips();
    renderDashboard();
  });
  $("#clearSlots").addEventListener("click", () => {
    state.prefs.slots.clear();
    savePrefs();
    renderChips();
    renderDashboard();
  });

  $("#resetSettings").addEventListener("click", async () => {
    const result = await invoke("reset_settings");
    if (result?.reset) {
      state.settingsApplied = false;
      state.pollRevision += 1;
      toast(`Broken settings kept as ${result.backup}`);
      poll();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (state.hotkeyCapturing) return captureHotkey(event);
    if (state.hotkeySaving || state.hotkey !== "" || event.key !== "F6" || event.repeat) return;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    event.preventDefault();
    state.running ? stop() : $("#startButton").click();
  });

  const start = async () => {
    showView();
    renderChips();
    renderDashboard();
    state.resetTimer = setInterval(updateResetClock, 30000);
    loadChecks();
    await poll();
    loadStreak();
    renderSchedule(await invoke("get_schedule", undefined, { quiet: true }));
    renderEmbedPreview();
    const launch = await invoke("get_launch_info", undefined, { quiet: true });
    if (launch?.version) $("#appVersion").textContent = `· v${launch.version}`;
    // Scheduled runs start in the background, so show them as the pill.
    if (launch?.scheduled) setCompact(true);
  };

  window.addEventListener("beforeunload", () => {
    state.unloaded = true;
    clearTimeout(state.poll);
    clearTimeout(prefsTimer);
    clearTimeout(planTimer);
    clearTimeout(toast.timer);
    clearInterval(state.resetTimer);
  });

  // pywebview injects its bridge after the page loads.
  if (api()) start();
  else window.addEventListener("pywebviewready", start, { once: true });
})();
