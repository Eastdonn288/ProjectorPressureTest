// PPTP frontend - vanilla JS, no framework.
// Interaction model:
//   1. Each device card has its own script dropdown (per-device state)
//   2. Selecting a script in one device's dropdown does NOT affect other devices
//   3. Clicking "Run" runs that device's selected script
//   4. Clicking a task card opens its log in the console panel
//   5. Clicking a device card body opens its current/last task log
//   6. The scripts panel is a read-only reference (no click action)

(() => {
  "use strict";

  // ---------------- State ----------------
  const state = {
    devices: [],
    scripts: [],
    tasks: [],
    // Per-device remembered script: { [serial]: filename }.
    // Each device is strictly independent - selecting a script for A
    // does not affect B, C, D, ...
    deviceScripts: {},
    // Per-device in-memory sequence content (ini text). Pre-filled when the
    // sequence editor modal is opened from a device card. Not persisted to
    // disk; cleared on full page reload.
    deviceSequences: {},
    // Per-device, per-script params config: { [serial]: { [script]: {k:v} } }.
    // Each (device, script) pair is strictly independent - configuring
    // wifi_onoff for device A never affects wifi_reboot or device B.
    deviceParams: {},
    // The currently selected device (last device card clicked). The script
    // panel shows the script for THIS device (highlighted + pinned to top).
    // Other devices' selections are stored in state.deviceScripts but not
    // surfaced in the UI - they're per-device, surfaced on demand.
    selectedDeviceSerial: null,
    currentDeviceSerial: null,
    currentTaskId: null,
    // PERF realtime chart (v2.4.0): per-task sample data bound to currentTaskId.
    // perfMeta holds the PERF| meta line of the CURRENT task; perfSamples holds
    // per-task sample buffers (survive task switches, cleared on delete/reset).
    perfMeta: null,       // { taskId, sources, track_fg, ... }
    perfSamples: {},      // { [taskId]: [ {t,cpu,gpu,mem,fg_cpu,fg_pkg,gpu_clk} ] }
    currentWs: null,
    backendOnline: false,
    userScrolledUp: false,
    server: null,
    shuttingDown: false,
    // Modal context: { source: "file" | "device", file?: "default", device?: serial }
    seqContext: null,

    // ----- Log capture (v2.6.0, display dropped in v2.7.0) -----
    // The server still captures logcat (always) and the serial console (opt-in)
    // for every task, but the frontend renders ONLY stdout - permanently. The
    // device channels are archived to disk and never displayed, so there is no
    // channel state here at all. See archiveChartIfAvailable() for the one bit
    // of the archive the browser still contributes.
    //
    // Per-device serial-capture opt-in (the run-time control on the device card).
    deviceSerialCapture: {},   // { [serial]: bool }
    deviceSerialPort: {},      // { [serial]: "COM9" }
    serialPorts: [],           // cached COM port list from /api/serial/ports
    // Per-device logcat opt-OUT (v2.7.5): absent or true = capture, only an
    // explicit false turns it off. Deliberately NOT persisted, for the same
    // reason as the serial checkbox - it is a per-run intent, and a stale "off"
    // would silently cost the crash trail on the next stress run.
    deviceLogcatCapture: {},   // { [serial]: false } = opted out
    // Archive occupancy for the tasks panel header (GET /api/archive/stats).
    archiveStats: null,        // { count, bytes }
    // Task ids we have already tried to POST a rendered chart for, so the 2s
    // poll does not re-upload on every tick.
    chartPosted: {},
  };

  // Cache last render keys to skip unnecessary re-renders
  const _lastKey = { devices: "", scripts: "", tasks: "" };

  // ----- PERF realtime chart (v2.4.0) -----
  // The chart (#perf-chart) lives in the LOG panel between #log-info and the
  // console, bound to state.currentTaskId. Switching tasks disposes + hides /
  // re-inits the chart on the same div - zero residue. All ECharts work happens
  // at discrete navigation points (task open / WS onopen / delete / reset) and
  // on the rAF log flush - NOT in the 2s render poll.
  // Device-side capture channels that exist on the server and in the archive,
  // but are never rendered here. Used only by the (currently hidden) export
  // path and by the archive summary line the server pushes.
  const CAPTURE_EXPORT = ["logcat", "serial"];

  const PERF_PREFIX = "PERF|";
  const PERF_SAMPLE_CAP = 86400;  // per-task sample buffer (24h @2s; long runs keep full history)
  const PERF_WINDOW_MS = 60 * 60 * 1000;  // rolling 1-hour chart x-axis window (6 ticks x 10min)
  let perfChart = null;           // current echarts instance
  let perfChartTaskId = null;     // task the chart is currently bound to
  let perfPending = [];           // batched samples awaiting chart flush
  let perfOption = null;          // current chart option (series data mutated in place)
  let perfSeriesKeys = [];        // series keys in chart order, e.g. ["cpu","gpu","mem"]
  // Wall-clock x-axis: perfXBase = task.started_at epoch ms, so a sample's
  // x-coord = s.clock (script-provided) or perfXBase + s.t*1000 (fallback for
  // pre-v2.4.1 logs). fmtClock renders HH:MM:SS (local time) on the x-axis.
  let perfXBase = Date.now();
  const fmtClock = (ms) =>
    new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
  const perfX = (s) => {
    if (s && s.clock != null) return s.clock;
    return perfXBase + (s && s.t != null ? s.t * 1000 : 0);
  };

  // ---------------- DOM helpers ----------------
  const $ = (id) => document.getElementById(id);
  const fmtTime = (s) => s ? new Date(s).toLocaleTimeString() : "—";
  const fmtDuration = (start, end) => {
    if (!start) return "—";
    const s = new Date(start).getTime();
    const e = end ? new Date(end).getTime() : Date.now();
    const ms = Math.max(0, e - s);
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${Math.floor(ms / 60000)}m${Math.floor((ms % 60000) / 1000)}s`;
  };
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));

  function setHint(elId, text) {
    const el = $(elId);
    if (el) el.textContent = text;
  }

  // ---------------- REST helpers ----------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      // Try to extract FastAPI's `detail` field for a friendly message
      let msg = `${res.status} ${res.statusText}`;
      try {
        const data = await res.json();
        if (data && data.detail) msg = data.detail;
      } catch (_) {
        // not JSON, fall back to raw text
        try {
          const text = await res.text();
          if (text) msg += `: ${text}`;
        } catch (_) {}
      }
      throw new Error(msg);
    }
    return res.json();
  }

  // ---------------- Data diff ----------------
  function stableKey(obj) {
    return JSON.stringify(obj);
  }

  function devicesRenderKey() {
    return stableKey({
      devices: state.devices,
      tasks: state.tasks.map((t) => ({
        device: t.device, status: t.status, task_id: t.task_id, script: t.script,
      })),
      deviceScripts: state.deviceScripts,
      // Serial opt-in controls are re-rendered from state, so their changes
      // must invalidate the key or the checkbox would snap back on the 3s poll.
      serialCapture: state.deviceSerialCapture,
      serialPort: state.deviceSerialPort,
      serialPorts: state.serialPorts,
      logcatCapture: state.deviceLogcatCapture,
      scriptMap: Object.fromEntries(state.scripts.map((s) => [s.filename, s.name])),
    });
  }
  function scriptsRenderKey() {
    return stableKey({
      scripts: state.scripts,
    });
  }
  function tasksRenderKey() {
    return stableKey({ tasks: state.tasks, current: state.currentTaskId });
  }

  // ---------------- Render: Scripts (read-only reference) ----------------
  // The script panel is a "view" of the currently selected device's script.
  // - Highlighting is per-device: only state.selectedDeviceSerial's script
  //   gets pinned + highlighted. Other devices' selections exist in state
  //   but are not surfaced in the UI here.
  // - Clicking a card opens its config UI (currently: ir_runner only).
  function renderScripts() {
    const el = $("script-list");
    if (!state.scripts.length) {
      el.innerHTML = `<div class="empty">
        scripts/ 目录无脚本<br>
        <small>把 *.py 放到 scripts/ 即可</small>
      </div>`;
      setHint("script-hint", "暂无脚本");
      return;
    }

    const selectedDevice = state.selectedDeviceSerial;
    const selectedScriptName = selectedDevice
      ? state.deviceScripts[selectedDevice] || null
      : null;
    // Bug1: a card is "locked" when the selected device currently has a
    // running task — the user shouldn't re-trigger Run or open the config
    // modal mid-run. (Decision is per the selected device, not per task.)
    const selectedDeviceRunning = !!(
      selectedDevice &&
      state.tasks.find(
        (t) => t.device === selectedDevice &&
               (t.status === "running" || t.status === "interrupting")
      )
    );
    // Bug3: a selected device is "reachable" if it shows up in adb devices
    // OR has a running task (temp-offline case). When unreachable AND not
    // running, we suppress the active highlight so stale "current script"
    // doesn't linger for an offline device.
    const selectedDeviceReachable = !selectedDevice || state.devices.some(
      (d) => d.serial === selectedDevice
    ) || selectedDeviceRunning;

    el.innerHTML = state.scripts.map((s) => {
      // Per-card state: which cards are active (clickable + has Run button)
      // vs disabled (no device selected, or device's script != this one)
      // vs locked (active card but device is currently running).
      const hasConfig = s.filename === "ir_runner.py";
      // Scripts that declare params (via --dump-params) get a params modal
      // on card click, mirroring ir_runner's sequence picker.
      const hasParamsConfig = !!s.has_params;
      // Bug3: only highlight if the device is still reachable.
      const isActive = selectedDeviceReachable && selectedScriptName === s.filename;
      const isLocked = isActive && selectedDeviceRunning;
      const disabled = !selectedDevice || !isActive || isLocked;
      const classes = [
        "script-card",
        isLocked ? "script-card-running"
        : isActive ? "script-card-active"
        : "script-card-disabled",
      ].join(" ");

      const iconChar = isActive ? "📌" : "📜";

      // Subtitle hints
      let subtitle;
      let cardTitle;
      if (!selectedDevice) {
        subtitle = "先在设备卡上选一台设备,再点此卡选择序列";
        cardTitle = "无设备选中 - 不可点击";
      } else if (isActive) {
        // This card is the active one
        const boundSeq = state.deviceSequences[selectedDevice];
        if (hasConfig) {
          subtitle = boundSeq
            ? `设备 ${selectedDevice} · 序列: ${boundSeq}.ini`
            : `设备 ${selectedDevice} · 未选序列 (点此卡选择)`;
        } else if (hasParamsConfig) {
          const bp = state.deviceParams?.[selectedDevice]?.[s.filename];
          subtitle = bp && Object.keys(bp).length
            ? `设备 ${selectedDevice} · 参数已配置 (点此卡修改)`
            : `设备 ${selectedDevice} · 未配置参数 (点此卡配置)`;
        } else {
          subtitle = `设备 ${selectedDevice} 已选此脚本`;
        }
        cardTitle = `设备 ${selectedDevice} 已选 ${s.name}`;
      } else {
        // Device is selected, but its script is different
        const otherDev = selectedDevice;
        const otherScript = selectedScriptName || "(未选)";
        subtitle = `设备 ${otherDev} 选了 ${otherScript},不是 ${s.name}`;
        cardTitle = `设备 ${otherDev} 当前脚本是 ${otherScript},不是此卡`;
      }

      // Run button on the active card (if conditions allow)
      let runBtnHtml = "";
      if (isActive) {
        const needsSeq = hasConfig && !state.deviceSequences[selectedDevice];
        // Bug1: disable Run when device is currently running — already running,
        // can't re-run. (The card stays clickable so user can re-pick a
        // sequence if they want, but Run is hard-disabled.)
        const runDisabled = needsSeq || isLocked;
        const runTitle = isLocked ? "设备在跑任务,不能重复跑(等结束或中断)"
                      : needsSeq  ? "先选序列(点此卡打开 picker)"
                      :              "在此设备上运行此脚本";
        runBtnHtml = `
          <div class="script-card-run">
            <button class="btn btn-primary btn-run-script"
                    data-script="${esc(s.filename)}"
                    ${runDisabled ? "disabled" : ""}
                    title="${esc(runTitle)}">
              ▶ 跑
            </button>
          </div>`;
      }

      return `
        <div class="${classes}" data-script="${esc(s.filename)}"
             data-has-config="${hasConfig}"
             data-has-params="${hasParamsConfig}"
             data-active="${isActive}"
             data-locked="${isLocked}"
             title="${esc(cardTitle)}">
          <div class="script-icon">${iconChar}</div>
          <div class="script-body">
            <div class="script-title">${esc(s.name)}</div>
            <div class="script-sub">${esc(subtitle)}</div>
          </div>
          ${runBtnHtml}
        </div>`;
    }).join("");

    // Click handler: only ACTIVE + non-locked cards are clickable, and they
    // open the sequence picker. Locked cards (active but device is running)
    // are visually dimmed and click no-op.
    el.querySelectorAll(".script-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (card.dataset.active !== "true") return;     // disabled cards: no-op
        if (card.dataset.locked === "true") return;     // Bug1: locked cards ignore clicks
        if (e.target.closest(".btn-run-script")) return; // Run button has its own handler
        const filename = card.dataset.script;
        if (card.dataset.hasConfig === "true" && filename === "ir_runner.py") {
          openSeqModal({ device: state.selectedDeviceSerial });
        } else if (card.dataset.hasParams === "true") {
          openParamsModal({ device: state.selectedDeviceSerial, script: filename });
        }
      });
    });
    // Run button: runs the active card's script on the currently-selected device
    el.querySelectorAll(".btn-run-script").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        onRunClick();
      });
    });

    // Hint: tell user which device's selection drives the highlight
    if (selectedDevice) {
      const devLabel = selectedScriptName
        ? `设备 ${selectedDevice} 选中: ${selectedScriptName}`
        : `设备 ${selectedDevice} 尚未选脚本`;
      setHint("script-hint", devLabel);
    } else {
      setHint("script-hint",
        `${state.scripts.length} 个可用脚本 · 先点设备卡选中一台`);
    }
  }

  // Latest task for a device, by started_at descending (null if none).
  function latestTaskOf(serial) {
    return [...state.tasks]
      .filter((t) => t.device === serial)
      .sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""))[0] || null;
  }

  // ---------------- Render: Devices ----------------
  function renderDevices() {
    const el = $("device-list");
    const liveSerials = new Set(state.devices.map((d) => d.serial));

    // Fix 1: synthesize "temporarily offline" devices for tasks that are live
    // but whose device is not in the current `adb devices` list. This lets the
    // user see the device card during planned ADB outages (e.g., reboot stress
    // tests where the script intentionally waits for the device to come back).
    // `interrupting` counts as live: pressing 中断 on a reboot test leaves the
    // script running out its wait for a device that is still absent, and the
    // card must not blink out of existence for those seconds.
    const tempOffline = state.tasks
      .filter((t) => (t.status === "running" || t.status === "interrupting")
                     && !liveSerials.has(t.device))
      .reduce((acc, t) => {
        if (!acc.find((d) => d.serial === t.device)) {
          acc.push({
            serial: t.device,
            status: "device",
            model: "(临时离线 · 任务在跑)",
            product: "",
            transport: "",
            _temp_offline: true,
          });
        }
        return acc;
      }, []);

    // Fix 2: battery tests end with the device powered off (discharge drains it
    // to shutdown -> `stop reason : power_off`). That would normally make the
    // card vanish from the list, which reads as "device disappeared". Keep a
    // dedicated card for it (mirrors the temp-offline card): a terminal
    // battery task whose device is no longer in `adb devices` and is still the
    // device's latest task. When the device comes back the normal card returns.
    const battOff = [];
    {
      const seen = new Set();
      for (const t of state.tasks) {
        if (t.script !== "battery_inout_stress.py") continue;
        if (!["finished", "interrupted", "failed"].includes(t.status)) continue;
        if (liveSerials.has(t.device)) continue;
        if (seen.has(t.device)) continue;
        const latest = latestTaskOf(t.device);
        if (!latest || latest.script !== "battery_inout_stress.py") continue;
        seen.add(t.device);
        const isDischarge = latest.params && latest.params.mode === "discharge";
        battOff.push({
          serial: t.device,
          status: "device",
          model: isDischarge ? "(放电测试 · 设备已关机)" : "(电池测试 · 设备已关机)",
          product: "",
          transport: "",
          _batt_off: true,
        });
      }
    }

    if (!state.devices.length && !state.tasks.some((t) => t.status === "running") && !battOff.length) {
      el.innerHTML = `<div class="empty">
        未检测到 ADB 设备<br>
        <small>请确认 adb 已安装并连接</small>
      </div>`;
      return;
    }

    const allDevices = [...state.devices, ...tempOffline, ...battOff];

    el.innerHTML = allDevices.map((d) => {
      const isTempOffline = !!d._temp_offline;
      const isBattOff = !!d._batt_off;
      const stClass = isBattOff ? "offline"
                    : isTempOffline ? "online"
                    : d.status === "device" ? "online"
                    : d.status === "unauthorized" ? "unauthorized"
                    : "offline";
      const stText = isBattOff ? "设备已关机"
                  : isTempOffline ? "临时离线(任务在跑)"
                  : d.status === "device" ? "在线"
                   : d.status === "unauthorized" ? "需授权"
                   : "离线";
      const running = state.tasks.find(
        (t) => t.device === d.serial && (t.status === "running" || t.status === "interrupting")
      );
      // Treat temp-offline / batt-off as "not offline" so user can still interact (e.g. force stop)
      const isOffline = !isTempOffline && !isBattOff && d.status !== "device";
      const isCurrentView = state.currentDeviceSerial === d.serial;
      const remembered = state.deviceScripts[d.serial] || null;

      // Per-device script dropdown - disabled when:
      // - device is NOT the currently selected one (user must click to select first)
      // - device has a running task (script locked during run)
      // - device is offline
      const notSelected = !isCurrentView;
      const dropdownDisabled = notSelected || !!running || isOffline;
      const optionsHtml = state.scripts.map((s) => {
        const sel = s.filename === remembered ? " selected" : "";
        return `<option value="${esc(s.filename)}"${sel}>${esc(s.name)}</option>`;
      }).join("");
      const dropdownHtml = `
        <select class="script-select" data-serial="${esc(d.serial)}"
                ${dropdownDisabled ? "disabled" : ""}
                title="${notSelected
                    ? `先点击设备 ${d.serial} 选中它,再选择脚本`
                    : `为 ${esc(d.serial)} 选择要运行的脚本`}">
          <option value="">— 选择脚本 —</option>
          ${optionsHtml}
        </select>`;

      // Serial-capture opt-in. The port is a per-device preference (remembered);
      // the checkbox is per-session, matching the existing split where
      // deviceParams persist but deviceScripts deliberately do not.
      const scriptMeta = state.scripts.find((s) => s.filename === remembered);
      const ownsSerial = !!(scriptMeta && scriptMeta.owns_serial);
      const wantSerial = !!state.deviceSerialCapture[d.serial];
      const portValue = state.deviceSerialPort[d.serial] || "";
      const serialDisabled = ownsSerial || dropdownDisabled;
      const serialTitle = ownsSerial
        ? "该脚本自身占用串口,平台串口抓取已自动让位(避免抢口导致电量曲线变平)"
        : notSelected ? `先点击设备 ${d.serial} 选中它`
        : running ? "任务运行中,串口抓取设置已锁定"
        : isOffline ? "设备离线"
        : "勾选后,本设备的下一次任务会同时采集串口 console 日志";
      const portOptions = state.serialPorts.map((p) => {
        const sel = p === portValue ? " selected" : "";
        return `<option value="${esc(p)}"${sel}>${esc(p)}</option>`;
      }).join("");
      // Highlight only when capture will ACTUALLY run: a checked box on an
      // unselected/locked device is not something to draw attention to.
      const serialActive = wantSerial && !serialDisabled;
      const serialHtml = `
        <div class="device-row4${serialActive ? " serial-on" : ""}">
          <label class="serial-opt" title="${esc(serialTitle)}">
            <input type="checkbox" class="serial-cap" data-serial="${esc(d.serial)}"
                   ${wantSerial ? "checked" : ""} ${serialDisabled ? "disabled" : ""}>
            <span${ownsSerial ? ' class="muted"' : ""}>串口日志</span>
          </label>
          <select class="serial-port" data-serial="${esc(d.serial)}"
                  ${serialDisabled || !wantSerial ? "disabled" : ""}
                  title="${esc(serialTitle)}">
            <option value=""${portValue ? "" : " selected"}>— COM —</option>
            ${portOptions}
          </select>
        </div>`;

      // Logcat opt-out (v2.7.5). Capture is the default and the control shows
      // the CURRENT intent, so an off-by-accident is visible on the card rather
      // than only discoverable from a missing log after the run.
      const wantLogcat = state.deviceLogcatCapture[d.serial] !== false;
      const logcatTitle = notSelected ? `先点击设备 ${d.serial} 选中它`
        : running ? "任务运行中,logcat 设置已锁定"
        : isOffline ? "设备离线"
        : wantLogcat
          ? "本设备的下一次任务会采集 logcat。压力测试建议保持开启(崩溃现场就在里面)"
          : "已关闭:本次任务不采集 logcat。长时间监控建议关闭 —— 一次 8 小时约 1GB,且没人会看";
      const logcatHtml = `
        <div class="device-row5">
          <label class="serial-opt" title="${esc(logcatTitle)}">
            <input type="checkbox" class="logcat-cap" data-serial="${esc(d.serial)}"
                   ${wantLogcat ? "checked" : ""} ${dropdownDisabled ? "disabled" : ""}>
            <span${wantLogcat ? "" : ' class="muted"'}>logcat 日志</span>
          </label>
        </div>`;

      let btnHtml = "";
      // Run button moved to the script card. Device card just shows status:
      // online/offline/running + (when a script is selected) the chip in
      // row2 shows the name. Always initialize to "" so we never render
      // literal "undefined" when no badge applies.
      if (running) {
        btnHtml = `<span class="badge-mini badge-running">运行中</span>`;
      } else if (isBattOff) {
        btnHtml = `<span class="badge-mini" style="background:rgba(248,113,113,0.18);color:var(--err);border-color:var(--err)">放电关机</span>`;
      } else if (isOffline) {
        btnHtml = `<span class="badge-mini" style="background:rgba(248,113,113,0.12);color:var(--err);border-color:var(--err)">离线</span>`;
      } else if (!remembered) {
        btnHtml = `<span class="muted" style="font-size:11px">未选脚本</span>`;
      }
      // else: empty string (chip in row2 shows the selected script name)

      const seqName = state.deviceSequences[d.serial];
      // Device card no longer shows the sequence picker - that lives on the
      // ir_runner script card in the scripts panel. The card itself is for
      // device-level state only: online/offline, selected script, run button.
      // Per-device bound sequence is surfaced via the ir_runner script card.
      return `
        <div class="device-card ${isCurrentView ? "active" : ""} ${isOffline ? "offline" : ""} ${isTempOffline ? "device-card-temp-offline" : ""} ${isBattOff ? "device-card-batt-off" : ""}"
             data-serial="${esc(d.serial)}">
          <div class="device-row1">
            <span class="dot ${stClass}"></span>
            <span class="device-name" title="${esc(d.serial)}">${esc(d.serial)}</span>
            <span class="muted">${esc(d.model || "")}</span>
          </div>
          <div class="device-row2">
            <span class="device-status">${stText}</span>
            ${remembered ? `<span class="device-script-chip" title="当前选中的脚本">${esc(remembered.replace(/\.py$/, ""))}</span>` : ""}
          </div>
          <div class="device-row3">${dropdownHtml}</div>
          ${serialHtml}
          ${logcatHtml}
          <div class="device-actions">${btnHtml}</div>
        </div>`;
    }).join("");

    // Per-device dropdown change
    el.querySelectorAll(".script-select").forEach((sel) => {
      sel.addEventListener("change", (e) => {
        const serial = sel.dataset.serial;
        const filename = sel.value || null;
        state.deviceScripts[serial] = filename;
        if (filename) {
          // Track which device is "active" so the script panel highlights
          // THIS device's selected script. (The script panel is bound to the
          // currently-selected device, not to all devices' selections.)
          state.selectedDeviceSerial = serial;
        }
        savePersistedState();
        renderDevices();
        // Re-render scripts to reflect pin + highlight changes
        renderScripts();
      });
      // Don't bubble up to card click (we already guard, but be safe)
      sel.addEventListener("click", (e) => e.stopPropagation());
    });

    // Serial-capture opt-in controls. State lives in `state` (not in the DOM)
    // because the card is re-rendered every 3s by the device poll.
    el.querySelectorAll(".serial-cap").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        e.stopPropagation();
        const serial = cb.dataset.serial;
        state.deviceSerialCapture[serial] = cb.checked;
        if (cb.checked && !state.deviceSerialPort[serial] && state.serialPorts.length === 1) {
          // Single-port hosts (the common case) should not need a second click.
          state.deviceSerialPort[serial] = state.serialPorts[0];
        }
        savePersistedState();
        renderDevices();
      });
      cb.addEventListener("click", (e) => e.stopPropagation());
    });
    // Logcat opt-out control. Not persisted (see state.deviceLogcatCapture), so
    // no savePersistedState() here - the intent is per run.
    el.querySelectorAll(".logcat-cap").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        e.stopPropagation();
        state.deviceLogcatCapture[cb.dataset.serial] = cb.checked;
        renderDevices();
      });
      cb.addEventListener("click", (e) => e.stopPropagation());
    });

    el.querySelectorAll(".serial-port").forEach((sel) => {
      sel.addEventListener("change", (e) => {
        e.stopPropagation();
        state.deviceSerialPort[sel.dataset.serial] = sel.value || "";
        savePersistedState();
      });
      sel.addEventListener("click", (e) => e.stopPropagation());
    });

    el.querySelectorAll(".device-card").forEach((c) => {
      c.addEventListener("click", (e) => {
        if (e.target.closest("button") || e.target.closest("select")
            || e.target.closest("label") || e.target.closest("input")) return;
        onSelectDevice(c.dataset.serial);
      });
    });
  }

  // The Chinese HTML report(s) a finished run archived, read straight off the
  // archive manifest (t.archive.files, server.py). Offered only when one
  // actually exists: an archived run predating v2.10.0 has none, and a button
  // that opens a 404 is worse than no button.
  function htmlReports(t) {
    return ((t.archive && t.archive.files) || [])
      .filter((f) => String(f).toLowerCase().endsWith(".html"));
  }

  function reportTitle(t) {
    const files = htmlReports(t);
    if (files.length <= 1) return `阅读本次运行的中文报告 ${files[0] || ""}`;
    // app_launch tests three apps and writes one report each. The route opens
    // the first, so say how many there are rather than implying there is one.
    return `阅读本次运行的中文报告(共 ${files.length} 份,此处打开第一份 ${files[0]})`;
  }

  // ---------------- Render: Tasks ----------------
  function renderTasks() {
    const el = $("task-list");
    if (!state.tasks.length) {
      el.innerHTML = `<div class="empty">暂无任务</div>`;
      return;
    }
    const sorted = [...state.tasks].sort((a, b) =>
      (b.started_at || "").localeCompare(a.started_at || "")
    );
    el.innerHTML = sorted.map((t) => {
      const isTerminal = ["finished", "failed", "interrupted"].includes(t.status);
      const cls = `status-badge status-${t.status}`;
      return `
        <div class="task-card ${isTerminal ? "history" : ""} ${state.currentTaskId === t.task_id ? "active" : ""}"
             data-task="${esc(t.task_id)}" title="点击查看日志">
          <div class="task-row1">
            <span class="device-name" title="${esc(t.device)}">${esc(t.device)}</span>
            <span class="task-row1-right">
              ${htmlReports(t).length
                ? `<button class="task-open task-report" data-report="${esc(t.task_id)}" title="${esc(reportTitle(t))}">报告</button>`
                : ""}
              ${t.archive
                ? `<button class="task-open" data-open="${esc(t.task_id)}" title="打开存档文件夹 archive/${esc(t.archive.module || "")}/${esc(t.archive.dir)}/">存档</button>`
                : ""}
              ${isTerminal
                ? `<button class="task-del" data-del="${esc(t.task_id)}" title="从列表移除(存档保留在 archive/)">×</button>`
                : ""}
              <span class="${cls}">${esc(t.status)}</span>
            </span>
          </div>
          <div class="task-sub" title="${esc(t.script)}">${esc(t.script)}</div>
          <div class="task-sub">
            起 ${fmtTime(t.started_at)} ·
            <span class="task-duration">${fmtDuration(t.started_at, t.ended_at)}</span>
          </div>
          ${t.exit_code !== null && t.exit_code !== undefined
            ? `<div class="task-sub">exit=${t.exit_code}</div>` : ""}
        </div>`;
    }).join("");

    el.querySelectorAll(".task-card").forEach((c) => {
      c.addEventListener("click", () => viewTaskLogs(c.dataset.task));
    });
    el.querySelectorAll(".task-open").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        onRevealArchive(b.dataset.open);
      });
    });
    el.querySelectorAll(".task-report").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        window.open(`/api/tasks/${b.dataset.report}/report`, "_blank");
      });
    });
    el.querySelectorAll(".task-del").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        onDeleteTask(b.dataset.del);
      });
    });
  }

  // ---------------- Logs ----------------
  function setConsoleTarget(taskId, deviceSerial) {
    state.currentTaskId = taskId;
    state.currentDeviceSerial = deviceSerial;
    renderLogInfo();
  }

  function renderLogInfo() {
    const el = $("log-info");
    const tid = state.currentTaskId;
    const t = state.tasks.find((x) => x.task_id === tid);
    if (!t) {
      const serial = state.currentDeviceSerial;
      el.innerHTML = serial
        ? `<span class="muted">查看设备 ${esc(serial)} 的日志(该设备暂无任务)</span>`
        : `<span class="muted">未选择任务</span>`;
      $("btn-stop-task").disabled = true;
      $("btn-hard-stop").disabled = true;
      return;
    }
    const lineCount = $("log-console").textContent.split("\n").length - 1;
    el.innerHTML = `
      <span class="device-name">${esc(t.device)}</span>
      <span class="muted">·</span>
      <span class="script-name">${esc(t.script)}</span>
      <span class="muted">·</span>
      <span class="status-badge status-${esc(t.status)}">${esc(t.status)}</span>
      <span class="muted">·</span>
      <span class="runtime">${fmtDuration(t.started_at, t.ended_at)}</span>
      <span class="muted">·</span>
      <span class="line-count">${lineCount} 行</span>
    `;
    const isLive = ["running", "interrupting"].includes(t.status);
    $("btn-stop-task").disabled = !isLive;

    // 硬停 button: only enabled when current task's device is in temp-offline
    // state (i.e., not in current adb devices list). Normal devices use 中断
    // which sends CTRL_BREAK.
    const liveSerials = new Set(state.devices.map((d) => d.serial));
    const isTempOffline = !liveSerials.has(t.device);
    $("btn-hard-stop").disabled = !(isLive && isTempOffline);
  }

  // ----- Log buffer (batches DOM updates via requestAnimationFrame) -----
  // Without batching, replaying 2000+ lines on every WS reconnect causes a
  // visible "刷" effect — textContent += on every line + scrollTop update
  // = O(n²) work and obvious flicker. Batching collapses N appends into a
  // single DOM update per animation frame.
  const logBuf = [];
  let logFlushScheduled = false;
  // Match VSCode's default terminal scrollback (1000 lines) — keeps the DOM
  // snappy while preserving enough recent context to read.
  const LOG_DOM_CAP = 1000;

  // The console shows stdout only. logcat/serial are still captured and
  // archived server-side, but the client never subscribes to them (see openWs),
  // so their frames do not even reach the browser - nothing to filter here.
  function appendLogLine(line) {
    // PERF| JSON lines are consumed by the chart layer; the readable line
    // replaces the raw JSON in the console. Malformed PERF lines pass through.
    if (typeof line === "string" && line.startsWith(PERF_PREFIX)) {
      line = handlePerfLine(line);
    }
    logBuf.push(line);
    if (logFlushScheduled) return;
    logFlushScheduled = true;
    requestAnimationFrame(flushLogBuffer);
  }

  function flushLogBuffer() {
    logFlushScheduled = false;
    flushPerfChart();   // PERF samples flush even if there is no log text this frame
    if (logBuf.length === 0) return;

    const con = $("log-console");
    if (con.textContent.startsWith("Click")) con.textContent = "";
    // hour12:false -> guaranteed 24h "HH:mm:ss" (a zh-CN locale can otherwise
    // emit Chinese AM/PM like "下午3:30" into the log, violating the ASCII rule).
    const ts = fmtClock(Date.now());
    // One textContent update for all buffered lines
    con.textContent += logBuf.map((line) => `[${ts}] ${line}\n`).join("");
    logBuf.length = 0;

    // Cap DOM size so the browser doesn't choke on huge logs
    const allLines = con.textContent.split("\n");
    if (allLines.length > LOG_DOM_CAP + 1) {
      const dropped = allLines.length - LOG_DOM_CAP - 1;
      const where = logFileName(state.currentTaskId);
      con.textContent = `(truncated ${dropped} lines; full log: ${where})\n`
                    + allLines.slice(-LOG_DOM_CAP).join("\n");
    }

    if (!state.userScrolledUp) con.scrollTop = con.scrollHeight;
    // Update line count in info bar (cheap query)
    const lcEl = $("log-info").querySelector(".line-count");
    if (lcEl) lcEl.textContent = `${con.textContent.split("\n").length - 1} 行`;
    $("btn-export-log").disabled = false;
  }

  // Where this task's stdout log lives, for the console-truncation note.
  // Server-authoritative: once a task is archived the file has moved into
  // archive/<dir>/, and only the server knows that name, so never rebuild it
  // here. tasks from before v2.6.0 carried no log_files map at all.
  function logFileName(tid) {
    if (!tid) return "logs/(no task)";
    const t = state.tasks.find((x) => x.task_id === tid);
    if (t && t.archive) {
      const m = t.archive.module ? `${t.archive.module}/` : "";
      return `archive/${m}${t.archive.dir}/stdout.log`;
    }
    if (t && t.log_files && t.log_files.stdout) return `logs/${t.log_files.stdout}`;
    return `logs/${tid}.log`;
  }

  function clearConsole() {
    $("log-console").textContent = "";
    const lcEl = $("log-info").querySelector(".line-count");
    if (lcEl) lcEl.textContent = "0 行";
    $("btn-export-log").disabled = true;
  }

  // ---------------- PERF realtime chart ----------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // PERF wire scripts power different chart types. "perf" = the performance
  // monitor (CPU/GPU/mem/fg); "battery" = the battery charge/discharge monitor
  // (level/temp). Returns null for tasks that don't stream PERF records.
  const PERF_SCRIPT_KINDS = {
    "perf_monitor.py": "perf",
    "battery_inout_stress.py": "battery",
  };
  function perfKind(taskId) {
    if (!taskId) return null;
    const t = state.tasks.find((x) => x.task_id === taskId);
    if (!t) return null;
    return PERF_SCRIPT_KINDS[t.script] || null;
  }
  function isPerfTask(taskId) {
    return perfKind(taskId) !== null;
  }

  // Perf event severity, mirroring the kinds perf_monitor.py can emit.
  //   alert = the data for this stretch is compromised, or the device is gone
  //   info  = a state change back to normal, or a plain fact worth dating
  //   (absent) = unknown kind; still shown, flagged with "?" - the script may
  //              ship a new kind before this table is updated, and silently
  //              dropping it would be worse than showing it unlabelled.
  const PERF_EVENT_LEVEL = {
    offline_start: "alert", timeout: "alert", misparse: "alert",
    frame_error: "alert", src_degraded: "alert",
    offline_end: "info", src_recovered: "info", reboot: "info",
    rollover: "info", pid_change: "info",
    // wifi_lost is an alert on purpose: without it the console shows "[?]" at
    // the exact moment the link dropped, which is the moment most worth
    // noticing. An unknown kind still renders as text (never raw JSON) - this
    // only decides the tag.
    wifi_lost: "alert", wifi_back: "info",
  };

  // Parse a PERF| line. Returns the human-readable line to show in the console
  // (the raw JSON is consumed). Malformed PERF lines return the original text.
  // The log console must stay ASCII/English (v2.5.1): no Chinese in what gets
  // printed to it - status/jump strings come straight from the script's ASCII
  // sample fields.
  function readableBattLine(obj) {
    const t = obj.t != null ? `${obj.t.toFixed(1)}s` : "-";
    const clk = obj.clock != null ? fmtClock(obj.clock) : fmtClock(perfX(obj));
    const lv = obj.level != null ? `${obj.level}%` : "-";
    const tp = obj.temp != null ? `${obj.temp.toFixed(1)}C` : "-";
    const vv = obj.voltage_mv != null ? `${(obj.voltage_mv / 1000).toFixed(2)}V` : "-";
    const st = obj.status || "-";
    let s = `[batt] ${clk} (${t}) level=${lv} temp=${tp} voltage=${vv} status=${st}`;
    if (obj.jump && obj.jump_type) {
      const prev = obj.level != null && obj.jump != null ? obj.level - obj.jump : null;
      s += ` [!] jump[${obj.jump_type}]${prev != null ? ` ${prev}->${obj.level}%` : ""}`;
    }
    return s;
  }

  // One fixed-width cell of a perf sample line. Constant width is the whole
  // point: a missing value must still occupy its column, otherwise every
  // following cell shifts left and the columns stop lining up.
  function padCell(v, unit, width, digits) {
    if (v == null) return "-".padStart(width);
    return (v.toFixed(digits) + unit).padStart(width);
  }

  // Per-metric state cell, "<tick result>:<9 state chars>". The result word
  // varies in length (ok / error / timeout / offline / partial) while the nine
  // state chars never do, so the result is right-aligned to 7 and the whole cell
  // is 7+1+9 = 17 wide. Without this a single offline tick shifts every column
  // after it by 6 and the vertical scan breaks - which is the one thing the
  // fixed-width form exists to prevent, and it breaks exactly when a fault is
  // happening and the columns matter most.
  function padSt(raw) {
    if (!raw) return "-".padStart(17);
    const i = raw.indexOf(":");
    if (i < 0) return String(raw).padStart(17);
    return raw.slice(0, i).padStart(7) + raw.slice(i);
  }

  function handlePerfLine(line) {
    let obj;
    try { obj = JSON.parse(line.slice(PERF_PREFIX.length)); }
    catch (_) { return line; }
    if (!obj || typeof obj !== "object") return line;
    const tid = state.currentTaskId;
    const kind = perfKind(tid);

    if (obj.type === "meta") {
      if (tid) state.perfMeta = Object.assign({ taskId: tid, kind }, obj);
      if (perfChart && perfChartTaskId === tid) {
        perfOption = buildPerfOption(tid);
        backfillPerfOption(tid);
        perfChart.setOption(perfOption, true);
      }
      if (kind === "battery") {
        const src = obj.sources || {};
        const modeName = src.mode === "discharge" ? "discharge" : "charge";
        return `[batt] monitor start | mode=${modeName} | port=${src.com_port || "(none)"} | source=dumpsys battery`;
      }
      const src = obj.sources || {};
      return `[perf] monitor start | CPU=${src.cpu ? "stat" : "N/A"} | MEM=${src.mem ? "meminfo" : "N/A"}`
           + ` | GPU=${src.gpu ? "mali/dvfs" : "N/A"} | FG=${src.fg ? "top" : "off"}`
           // src.wifi is absent on older archived runs, so a missing key reads
           // as "off" rather than crashing the meta line.
           + ` | WIFI=${src.wifi ? "cmd wifi status" : "off"}`
           // src.ntc is absent on older archived runs, exactly like src.wifi.
           + ` | NTC=${src.ntc ? "iio adc" : "off"}`;
    }

    if (obj.type === "sample") {
      perfPending.push(obj);   // flushed to the chart on the next rAF log flush
      if (kind === "battery") return readableBattLine(obj);
      // Fixed-width cells, so a long run can be scanned VERTICALLY - the eye
      // finds a trend break by column position instead of re-parsing each line.
      // Three rules, each load-bearing:
      //   - null renders as a same-width "-", so an offline tick reads as a row
      //     of dashes and is never mistaken for a real 0.0%. (Pitfall #14: on
      //     this board GPU ~= 0 while a video plays is CORRECT, so a row of
      //     zeroes must not be reachable by accident.)
      //   - pkg= goes LAST: package names have no fixed length and would push
      //     every following column out of alignment.
      //   - no wall clock: flushLogBuffer already prefixes [HH:MM:SS], and a
      //     second timestamp adds width without adding information.
      // Columns are always emitted even when a series is disabled (gpu without
      // root, fg with track_foreground off) - a dashed column stays aligned and
      // is self-explanatory, whereas a sometimes-present column is not.
      // "-" for a missing node, never 0: a zero ADC count is a real reading
      // (a shorted/absent NTC) and must not be reachable by accident.
      const ntcCell = (obj.ntc_lcd == null ? "-" : obj.ntc_lcd) + "/"
                    + (obj.ntc_led == null ? "-" : obj.ntc_led);
      let s = `[perf] t=${padCell(obj.t, "s", 8, 1)}`
            + ` | cpu=${padCell(obj.cpu, "%", 6, 1)}`
            + ` | gpu=${padCell(obj.gpu, "%", 6, 1)}`
            + ` | mem=${padCell(obj.mem, "%", 6, 1)}`
            + ` | fg=${padCell(obj.fg_cpu, "%", 6, 1)}`
            + ` | clk=${padCell(obj.gpu_clk, "MHz", 7, 0)}`
            + ` | st=${padSt(obj.st)}`
            // Fixed-width like every other cell, so a state word of another
            // length (noassoc / unknown = 7) cannot shift the columns after it
            // and break vertical reading. Deliberately NOT padCell(): that one
            // calls v.toFixed() and would throw on a string. Left-aligned
            // because this is a word, not a number. "-" for a sample with no
            // wifi reading (an older line replayed, or the channel disabled).
            + ` | wifi=${String(obj.wifi == null ? "-" : obj.wifi).padEnd(7)}`
            // Both NTC nodes in one cell, and it goes BEFORE pkg= for the reason
            // stated above: pkg has no fixed length. Fixed-width as well, sized
            // for a 12-bit full scale ("4095/4095" = 9, +2 of margin), and
            // deliberately NOT padCell() for the same reason wifi isn't - these
            // are raw ADC counts that can be missing, and padCell calls toFixed.
            // The numbers are NOT degrees; they are ADC counts (the script's
            // CSV declares unit=raw_adc). No chart is built from them - the
            // curve belongs to Excel, per the 2026-09-20 ruling.
            + ` | ntc=${String(ntcCell).padEnd(11)}`;
      if (obj.fg_pkg) s += ` | pkg=${obj.fg_pkg}`;
      return s;
    }

    // v2: mid-run events. These are FACTS observed during the run (the device
    // went offline, a counter rolled over), never conclusions - the verdict is
    // printed once, at the end. Rate limiting and de-duplication happen in the
    // script (it owns the clock), so whatever arrives here is worth one line.
    // Rendered as text, never as raw JSON: the console is replayed from stdout
    // on every WS reconnect, so a raw record would be re-shown in full each time.
    if (obj.type === "event") {
      const t = obj.t != null ? `${obj.t.toFixed(1)}s` : "-";
      const lvl = PERF_EVENT_LEVEL[obj.kind];
      const tag = lvl === "alert" ? "!" : (lvl === "info" ? "i" : "?");
      let s = `[perf] t=${t.padStart(8)} | [${tag}] ${obj.kind}`
            + (obj.reason ? `: ${obj.reason}` : "");
      if (obj.detail) s += ` (${obj.detail})`;
      return s;
    }

    // An unknown PERF| record must NOT be echoed. It is machine data, it can be
    // arbitrarily long, and it is re-sent on every reconnect - so a future
    // record type would flood the console with raw JSON the moment it ships.
    // One short notice is enough to make the gap visible without the payload.
    return `[perf] (unrecognized record type: ${String(obj.type || "?")})`;
  }

  // Append a sample to the per-task buffer, deduping on t (WS replay re-sends
  // the same samples when a task is re-viewed; identical t = already known).
  function pushPerfSample(taskId, s) {
    if (!state.perfSamples[taskId]) state.perfSamples[taskId] = [];
    const arr = state.perfSamples[taskId];
    const last = arr[arr.length - 1];
    if (last && last.t === s.t) return false;
    arr.push(s);
    if (arr.length > PERF_SAMPLE_CAP) arr.splice(0, arr.length - PERF_SAMPLE_CAP);
    return true;
  }

  function resetPerfBuffer(taskId) {
    perfPending = [];
    if (state.perfMeta && state.perfMeta.taskId !== taskId) state.perfMeta = null;
    syncPerfChart();
  }

  // Core no-residue logic: show/rebind/hide the chart based on the CURRENT task.
  // Called ONLY at discrete navigation points (task view, WS onopen, delete,
  // cleanup, reset) - never from the 2s render poll.
  function syncPerfChart() {
    const el = $("perf-chart");
    const tid = state.currentTaskId;
    const isPerf = isPerfTask(tid);
    const chartOk = typeof echarts !== "undefined";
    if (!isPerf || !chartOk) {
      if (perfChart) { perfChart.dispose(); perfChart = null; }
      perfChartTaskId = null;
      perfOption = null;
      el.hidden = true;
      return;
    }
    // Un-hide BEFORE init (a hidden div has zero size -> blank canvas)
    el.hidden = false;
    if (!perfChart || perfChartTaskId !== tid) {
      if (perfChart) { perfChart.dispose(); perfChart = null; }
      perfChart = echarts.init(el);
      perfChartTaskId = tid;
      perfOption = null;   // stale option from the previous task - force rebuild
    }
    // Anchor the wall-clock x-axis at this task's start time (fallback for
    // samples without a script-provided clock).
    const t = state.tasks.find((x) => x.task_id === tid);
    if (t && t.started_at) {
      const ms = new Date(t.started_at).getTime();
      if (!Number.isNaN(ms)) perfXBase = ms;
    }
    if (state.perfMeta && state.perfMeta.taskId !== tid) state.perfMeta = null;
    if (!perfOption) {
      perfOption = buildPerfOption(tid);
      backfillPerfOption(tid);
    }
    perfChart.setOption(perfOption, true);
    perfChart.resize();
    flushPerfChart();   // catch samples that arrived before the chart existed
  }

  // Build a fresh chart option for a task (empty series data). Determines which
  // series exist from the task's PERF meta (GPU hidden when the node is unreadable).
  // Right edge of the rolling x-axis window: the newest buffered sample's
  // clock, or the task-start anchor before any sample arrives.
  function perfWindowEnd(taskId) {
    const arr = state.perfSamples[taskId];
    if (arr && arr.length) return perfX(arr[arr.length - 1]);
    return perfXBase;
  }

  // Build the series list + keys for a task's chart (shared by the live 1h
  // window and the full-history PNG export). Determines which series exist from
  // the task's PERF meta (GPU hidden when the node is unreadable).
  function perfSeriesAndKeys(taskId) {
    const meta = state.perfMeta && state.perfMeta.taskId === taskId ? state.perfMeta : null;
    const src = (meta && meta.sources) || {};
    // meta.kind (set at meta receipt) is the safer source; fall back to resolving
    // the task by script name in case state.tasks isn't populated yet.
    const kind = (meta && meta.kind) || perfKind(taskId);
    const series = [];
    const keys = [];
    const mk = (name, color, key, yAxisIndex) => {
      series.push({
        name, type: "line", showSymbol: false, sampling: "lttb",
        connectNulls: false,
        lineStyle: { width: 1.5 }, itemStyle: { color },
        // fg_cpu (top's multi-core %CPU) can exceed 100% -> own right axis;
        // battery temp uses the right axis with its own °C scale.
        yAxisIndex,
        data: [],
      });
      keys.push(key);
    };
    if (kind === "battery") {
      // Battery chart: level (left, 0-100%) + temperature (right, auto °C).
      mk("电量", "#4f9eff", "level", 0);
      mk("温度", "#ef4444", "temp", 1);
    } else {
      mk("CPU", "#4f9eff", "cpu", 0);
      if (src.gpu) mk("GPU", "#4ade80", "gpu", 0);
      mk("MEM", "#fbbf24", "mem", 0);
      if (src.fg) mk("前台APP", "#f87171", "fg_cpu", 1);
    }
    return { series, keys, kind };
  }

  // Build a fresh chart option for a task (empty series data). Live usage: fixed
  // 1h rolling window (v2.5.1). `fullRange: true` (full-history PNG export)
  // spans the whole run and lets ECharts pick the tick step.
  function buildPerfOption(taskId, fullRange) {
    const { series, keys, kind } = perfSeriesAndKeys(taskId);
    perfSeriesKeys = keys;
    let xMin, xMax, xInterval, xSplit;
    if (fullRange) {
      const arr = state.perfSamples[taskId];
      if (arr && arr.length) {
        xMin = perfX(arr[0]);
        xMax = perfX(arr[arr.length - 1]);
        if (xMax - xMin < 1000) { xMin -= 500; xMax += 500; }  // single-sample run
      }
      // Exported PNG time axis: one tick every 0.5h (user requirement).
      // hideOverlap on axisLabel drops colliding labels on very long runs.
      xInterval = 30 * 60 * 1000;
    } else {
      // Fixed rolling 1-hour window: the axis always spans the last hour,
      // anchored to the newest sample, with exact 10-minute ticks (~6 splits,
      // "实时显示 1h"). Older points scroll off the left edge; the sample buffer
      // keeps full history for CSV/PNG export. `interval` pins the 10-min step
      // (splitNumber alone would let ECharts pick 5/15/30).
      xInterval = 10 * 60 * 1000;
      xSplit = 6;
      xMin = perfWindowEnd(taskId) - PERF_WINDOW_MS;
      xMax = perfWindowEnd(taskId);
    }
    const unitOf = { "电量": "%", "温度": "°C", "CPU": "%", "GPU": "%", "MEM": "%", "前台APP": "%" };
    const option = {
      backgroundColor: "#0a0d12",
      animation: false,
      grid: { left: 46, right: 56, top: 26, bottom: 26 },
      legend: { top: 2, textStyle: { color: "#8b93a3" }, data: series.map((s) => s.name) },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1d222b", borderColor: "#2a313d",
        textStyle: { color: "#d6dbe4" },
        formatter: (params) => {
          if (!params || !params.length) return "";
          const x = params[0].value[0];
          let s = fmtClock(x);
          if (x >= perfXBase) s += ` · t=${((x - perfXBase) / 1000).toFixed(1)}s`;
          for (const p of params) {
            const v = p.value[1];
            s += `<br/>${p.marker}${p.seriesName}: ${v == null ? "-" : v.toFixed(1) + (unitOf[p.seriesName] || "%")}`;
          }
          return s;
        },
      },
      xAxis: {
        type: "time",
        min: xMin,
        max: xMax,
        interval: xInterval,      // undefined in full-range -> auto ticks
        splitNumber: xSplit,
        axisLabel: { color: "#8b93a3", formatter: (v) => fmtClock(v), hideOverlap: true },
        splitLine: { lineStyle: { color: "#1d222b" } },
      },
      yAxis: kind === "battery" ? [
        {
          type: "value", min: 0, max: 100,
          axisLabel: { color: "#8b93a3", formatter: "{value}%" },
          splitLine: { lineStyle: { color: "#1d222b" } },
        },
        {
          type: "value", position: "right",
          axisLabel: { color: "#ef4444", formatter: "{value}°C" },
          splitLine: { show: false },
        },
      ] : [
        {
          type: "value", min: 0, max: 100,
          axisLabel: { color: "#8b93a3", formatter: "{value}%" },
          splitLine: { lineStyle: { color: "#1d222b" } },
        },
        {
          type: "value", min: 0, max: 400, position: "right",
          axisLabel: { color: "#f87171", formatter: "{value}%" },
          splitLine: { show: false },
        },
      ],
      // No dataZoom: the x-axis is a fixed rolling 1h window (v2.5.1); a zoom
      // control would let users drag out of the 1h view and fight the flush.
      series,
    };
    return option;
  }

  // Fill the current option's (empty) series with already-buffered samples.
  // Used on task re-view / replay rebuild so history reappears instantly.
  function backfillPerfOption(taskId) {
    const arr = state.perfSamples[taskId];
    if (!arr || !perfOption) return;
    for (let i = 0; i < perfSeriesKeys.length; i++) {
      const key = perfSeriesKeys[i];
      const data = perfOption.series[i].data;
      if (data.length > 0) continue;   // already has data - don't duplicate
      for (const s of arr) data.push([perfX(s), s[key] == null ? null : +s[key]]);
    }
  }

  // Flush batched samples (from perfPending) into the bound chart. Called at the
  // end of flushLogBuffer (rAF-batched), so a 50k-line replay renders as a few
  // setOption calls instead of one per line.
  function flushPerfChart() {
    if (perfPending.length === 0) return;
    if (!perfChart || perfChartTaskId !== state.currentTaskId) {
      perfPending.length = 0;   // dropped - chart is on another task
      return;
    }
    const tid = state.currentTaskId;
    const pending = perfPending.splice(0, perfPending.length);
    const fresh = [];
    for (const s of pending) if (pushPerfSample(tid, s)) fresh.push(s);
    if (!perfOption || fresh.length === 0) return;
    for (let i = 0; i < perfSeriesKeys.length; i++) {
      const key = perfSeriesKeys[i];
      const data = perfOption.series[i].data;
      for (const s of fresh) data.push([perfX(s), s[key] == null ? null : +s[key]]);
      if (data.length > PERF_SAMPLE_CAP) data.splice(0, data.length - PERF_SAMPLE_CAP);
    }
    // v2.5.1: roll the fixed 1h x-axis window forward to the newest sample,
    // and trim points that fell out of the window (keeps the chart light).
    const endX = perfX(fresh[fresh.length - 1]);
    const minX = endX - PERF_WINDOW_MS;
    if (perfOption.xAxis) {
      perfOption.xAxis.min = minX;
      perfOption.xAxis.max = endX;
    }
    for (let i = 0; i < perfSeriesKeys.length; i++) {
      const d = perfOption.series[i].data;
      while (d.length && d[0][0] < minX) d.shift();
    }
    perfChart.setOption(perfOption);
  }

  // Render a FULL-HISTORY PNG of a task's perf/battery data - NOT the 1h live
  // window - on a throwaway off-screen chart and return a data URL. The whole
  // sample buffer is plotted (lttb downsampling keeps long runs renderable), the
  // x-axis spans the entire run with auto ticks. Works even after switching away
  // from the task (perfSamples keeps everything; perfXBase is re-anchored to the
  // task's start so t-based fallbacks stay correct).
  function exportFullChartDataUrl(tid) {
    const arr = state.perfSamples[tid];
    if (!arr || !arr.length) return null;
    const savedBase = perfXBase;
    const t = state.tasks.find((x) => x.task_id === tid);
    if (t && t.started_at) {
      const ms = new Date(t.started_at).getTime();
      if (!Number.isNaN(ms)) perfXBase = ms;
    }
    let dataUrl = null;
    try {
      const option = buildPerfOption(tid, true);
      for (let i = 0; i < perfSeriesKeys.length; i++) {
        const key = perfSeriesKeys[i];
        const data = option.series[i].data;
        for (const s of arr) data.push([perfX(s), s[key] == null ? null : +s[key]]);
      }
      const div = document.createElement("div");
      div.style.cssText = "position:fixed;left:-9999px;top:0;width:1280px;height:360px;";
      document.body.appendChild(div);
      try {
        const chart = echarts.init(div);
        chart.setOption(option, true);
        dataUrl = chart.getDataURL({ pixelRatio: 2, backgroundColor: "#0a0d12" });
        chart.dispose();
      } finally {
        document.body.removeChild(div);
      }
    } catch (_) {
      dataUrl = null;
    } finally {
      perfXBase = savedBase;
    }
    return dataUrl;
  }

  function buildPerfCsv(task, samples) {
    const kind = perfKind(task.task_id);
    const L = [];
    L.push(kind === "battery" ? "# PPTP 电池采样导出" : "# PPTP 性能采样导出");
    L.push(`# 任务: ${task.task_id}`);
    L.push(`# 设备: ${task.device}`);
    L.push(`# 脚本: ${task.script}`);
    L.push(`# 状态: ${task.status}`);
    if (task.params && Object.keys(task.params).length) {
      L.push(`# 参数: ${JSON.stringify(task.params)}`);
    }
    if (kind === "battery") {
      L.push("t_sec,t_wall,level_percent,temp_c,voltage_mv,status,jump_delta,jump_type");
      for (const s of samples) {
        const f = (v) => (v == null ? "" : v);
        const wall = fmtClock(s.clock != null ? s.clock : perfX(s));
        L.push([f(s.t), wall, f(s.level), f(s.temp), f(s.voltage_mv),
                s.status || "", f(s.jump), s.jump_type || ""].join(","));
      }
    } else {
      L.push("t_sec,t_wall,cpu_percent,gpu_percent,mem_percent,fg_cpu_percent,fg_pkg,gpu_clk_mhz");
      for (const s of samples) {
        const f = (v) => (v == null ? "" : v);
        const wall = fmtClock(s.clock != null ? s.clock : perfX(s));
        L.push([f(s.t), wall, f(s.cpu), f(s.gpu), f(s.mem), f(s.fg_cpu),
                s.fg_pkg || "", f(s.gpu_clk)].join(","));
      }
    }
    return L.join("\n") + "\n";
  }

  function downloadBlob(content, filename, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function onExportLog() {
    const t = state.tasks.find((x) => x.task_id === state.currentTaskId);
    const tid = state.currentTaskId;
    if (!t || !tid) {
      alert("当前未选择任务,无可导出内容");
      return;
    }

    // Fetch FULL log from server (not just the capped DOM). The DOM only
    // shows the last ~1000 lines (VSCode-style scrollback); export should
    // contain everything ever written to the log file.
    let fullLines = [];
    try {
      const data = await api(`/api/tasks/${tid}/log`);
      fullLines = data.lines || [];
    } catch (e) {
      alert(`读取日志失败: ${e.message}`);
      return;
    }
    if (fullLines.length === 0) {
      alert("日志为空,无可导出内容");
      return;
    }

    // Filenames carry device + script + time so an exported file is
    // self-describing once it leaves this machine.
    const base = exportBaseName(t);

    let header = "=== PPTP 任务日志导出 ===\n";
    header += `导出时间: ${new Date().toISOString()}\n`;
    header += `任务ID:   ${t.task_id}\n`;
    header += `设备:     ${t.device}\n`;
    header += `脚本:     ${t.script}\n`;
    header += `状态:     ${t.status}\n`;
    header += `通道:     stdout\n`;
    if (t.params && Object.keys(t.params).length) {
      header += `参数:     ${JSON.stringify(t.params)}\n`;
    }
    header += `开始:     ${t.started_at || "-"}\n`;
    if (t.ended_at) header += `结束:     ${t.ended_at}\n`;
    if (t.exit_code !== null && t.exit_code !== undefined) {
      header += `退出码:   ${t.exit_code}\n`;
    }
    header += `总行数:   ${fullLines.length}\n`;
    const capSummary = capturesSummary(t);
    if (capSummary) header += `抓取:     ${capSummary}\n`;
    header += "\n=== 日志内容 ===\n";

    downloadBlob(header + fullLines.join("\n") + "\n",
                 `${base}.stdout.log`, "text/plain;charset=utf-8");

    // Device-side channels (logcat / serial), one file each. Skipped when the
    // channel never produced anything, so an export does not litter empty files.
    // NOTE: this whole export path is dormant in v2.7.0 - archiving replaced it
    // and #btn-export-log is hidden. Kept working in case it comes back.
    for (const chId of CAPTURE_EXPORT) {
      const info = (t.captures || {})[chId];
      if (!info) continue;
      let extra = null;
      try {
        const d = await api(`/api/tasks/${tid}/log?source=${chId}`);
        if (d.lines && d.lines.length) extra = d;
      } catch (_) { /* channel missing on disk - just skip it */ }
      if (!extra) continue;
      const where = t.archive
        ? `archive/${t.archive.dir}/${chId}.log`
        : `logs/${(t.log_files || {})[chId] || chId}`;
      let h = `=== PPTP ${chId} 通道导出 ===\n`;
      h += `导出时间: ${new Date().toISOString()}\n`;
      h += `任务ID:   ${t.task_id}\n`;
      h += `设备:     ${t.device}\n`;
      h += `脚本:     ${t.script}\n`;
      h += `通道状态: ${info.status || "unknown"}${info.detail ? ` (${info.detail})` : ""}\n`;
      h += `行数:     ${extra.lines.length}\n`;
      if (extra.truncated) {
        h += `注意:     文件较大,仅导出末尾部分(完整日志见 ${where})\n`;
      }
      h += "\n=== 日志内容 ===\n";
      await sleep(300);   // Firefox blocks a rapid 2nd/3rd download
      downloadBlob(h + extra.lines.join("\n") + "\n",
                   `${base}.${ch.id}.log`, "text/plain;charset=utf-8");
    }

    // v2.4.0: perf_monitor tasks export CSV + chart PNG alongside the txt log,
    // all downloaded from this one button (no zip). Chrome/Edge allow
    // multiple downloads in one gesture; Firefox may block the 2nd/3rd.
    const perfArr = state.perfSamples[tid];
    if (perfArr && perfArr.length > 0) {
      await sleep(300);
      const csvTag = perfKind(tid) === "battery" ? "batt" : "perf";
      downloadBlob(buildPerfCsv(t, perfArr), `${base}.${csvTag}.csv`, "text/csv;charset=utf-8");
      await sleep(300);
      // Full-history PNG (v2.5.1): renders a throwaway off-screen chart from the
      // whole sample buffer - NOT the 1h live window - so the exported image
      // covers the entire run (perf/battery alike). No longer requires the task
      // to still be the current view.
      const dataUrl = exportFullChartDataUrl(tid);
      if (dataUrl) {
        const a2 = document.createElement("a");
        a2.href = dataUrl;
        a2.download = `${base}.chart.png`;
        document.body.appendChild(a2);
        a2.click();
        document.body.removeChild(a2);
      }
    }
  }

  // Shared export filename stem: pptp_<script>_<device>_<utc timestamp>.
  function exportBaseName(t) {
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const clean = (s) => String(s || "").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 32);
    return `pptp_${clean((t.script || "").replace(/\.py$/, ""))}_${clean(t.device)}_${ts}`;
  }

  // One-line human summary of what was captured, for the export header.
  function capturesSummary(t) {
    const caps = t.captures || {};
    const ids = CAPTURE_EXPORT.filter((id) => caps[id]);
    if (!ids.length) return "";
    return ids.map((id) => {
      const info = caps[id];
      return `${id}=${info.status || "?"}${info.detail ? `(${info.detail})` : ""}`;
    }).join(" ");
  }

  // ---------------- Archive (v2.7.0) ----------------

  // Render this task's perf chart and hand it to the server, which writes it
  // into the task's archive folder.
  //
  // The server has no chart renderer on purpose (ECharts lives only in this
  // page, and pulling in matplotlib would mean maintaining two renderers for
  // one picture), so THIS is the only way a chart.png ever comes into being.
  // If no browser is open when a task ends there is simply no chart in the
  // archive - the CSV/report/stdout all survive, and summary.json says so.
  //
  // Best-effort: every failure is swallowed. An upload problem must never
  // disturb the console the user is reading.
  async function archiveChartIfAvailable(tid) {
    if (!tid || state.chartPosted[tid]) return;
    const t = state.tasks.find((x) => x.task_id === tid);
    if (!t || !t.archive) return;                       // not archived yet
    if ((t.archive.files || []).includes("chart.png")) return;  // already has one
    const arr = state.perfSamples[tid];
    if (!arr || !arr.length) return;                    // nothing to draw
    // Claim before awaiting so the 2s poll can't fire a second upload while the
    // first is still in flight. One attempt per task per page load.
    state.chartPosted[tid] = true;
    try {
      const dataUrl = exportFullChartDataUrl(tid);
      if (!dataUrl) return;
      await api(`/api/tasks/${tid}/archive/artifact`, {
        method: "POST",
        body: JSON.stringify({ name: "chart.png", data: dataUrl }),
      });
      // Reflect it locally so the card/occupancy update without a reload.
      if (t.archive) {
        t.archive.files = Array.from(new Set([...(t.archive.files || []), "chart.png"]));
      }
      refreshArchiveStats();
    } catch (_) { /* best effort - the run's data is safe on disk regardless */ }
  }

  // Open a task's archive folder. Local single-user tool, so this is the server
  // telling the OS to show a directory - no path is ever sent from here, the
  // server derives it from the task id.
  async function onRevealArchive(tid) {
    try {
      await api(`/api/tasks/${tid}/archive/reveal`, { method: "POST" });
    } catch (e) {
      appendLogLine(`[archive] could not open folder: ${e.message}`);
    }
  }

  // Open the archive root (the tasks-panel button).
  async function onRevealArchiveRoot() {
    try {
      await api("/api/archive/reveal", { method: "POST" });
    } catch (e) {
      appendLogLine(`[archive] could not open folder: ${e.message}`);
    }
  }

  // Total archive occupancy, shown in the tasks panel header. Cheap (a
  // directory walk) and on the 5s server-status cadence, not the 2s one.
  async function refreshArchiveStats() {
    try {
      const s = await api("/api/archive/stats");
      state.archiveStats = s;
      const el = $("archive-hint");
      if (el) {
        el.textContent = s.count
          ? `存档 ${fmtBytes(s.bytes)} · ${s.count} 个`
          : "暂无存档";
        // Per-module breakdown on hover - the archive is grouped by module
        // (archive/wifi/…, archive/perf/…), so this is the fastest way to see
        // where the disk went without opening Explorer.
        const mods = Object.entries(s.modules || {})
          .sort((a, b) => b[1].bytes - a[1].bytes)
          .map(([m, v]) => `${m}: ${fmtBytes(v.bytes)} (${v.count})`);
        el.title = [s.dir || "", ...mods].filter(Boolean).join("\n");
      }
    } catch (_) { /* ignore - occupancy is cosmetic */ }
  }

  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
  }

  async function viewTaskLogs(taskId) {
    const t = state.tasks.find((x) => x.task_id === taskId);
    if (!t) return;
    // Already viewing this task - don't close+reopen WS (would trigger reconnect loop)
    if (state.currentTaskId === taskId) {
      renderTasks(); // refresh active highlight
      renderScripts();  // Bug2: re-render scripts too (running status may have changed)
      return;
    }
    setConsoleTarget(taskId, t.device);
    // Bug2: switching tasks implicitly switches "focus" to that task's device.
    // Without this, the scripts panel keeps showing the previously-selected
    // device's active highlight, and you have to click the device card to fix it.
    state.selectedDeviceSerial = t.device;
    savePersistedState();
    state.userScrolledUp = false;
    clearConsole();
    // User-initiated switch: reset reconnect backoff so we get fresh attempts
    state._wsReconnectAttempts = 0;
    openWs(taskId);
    syncPerfChart();   // perf chart show/hide + rebind for the newly-viewed task
    renderTasks();     // refresh active task highlight
    renderDevices();   // Bug2: sync device card active highlight to t.device
    renderScripts();   // Bug2 + Bug3: re-evaluate which script is active
                       // (also Bug3 — if t.device is offline + non-running,
                       //  isActive becomes false via selectedDeviceReachable)
  }

  // stdout only, permanently (v2.7.0). The server accepts ?sources= and will
  // stream logcat/serial to anyone who asks, but nobody asks: subscribing to
  // just stdout is what keeps those channels off the wire entirely.
  function openWs(taskId) {
    if (state.currentWs) {
      try { state.currentWs.close(); } catch (_) {}
      state.currentWs = null;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(
      `${proto}//${location.host}/ws/logs/${taskId}?sources=stdout`
    );
    ws._taskId = taskId;
    state.currentWs = ws;
    ws.onopen = () => {
      // Successful connection - reset backoff counter
      state._wsReconnectAttempts = 0;
      // Stale WS: user already switched to another task - don't touch its state
      if (state.currentTaskId !== taskId) return;
      // Replay is about to stream: rebuild the perf chart for this task from
      // its buffered samples (task re-view); live samples append as they come.
      resetPerfBuffer(taskId);
      syncPerfChart();
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        // Defensive: if user switched away from this task while WS was still
        // open (e.g. onSelectDevice on a device with no tasks), drop frames
        // silently instead of dumping them into the (now-empty / another
        // task's) console. Fix for "切设备时旧设备的日志仍出现".
        if (state.currentTaskId !== ws._taskId) return;
        if (msg.type === "log") appendLogLine(msg.line);
        else if (msg.type === "end") {
          // Flush any buffered lines BEFORE end so they're all visible
          flushLogBuffer();
          refreshTasks();
          // The task just finished: render its perf chart from the samples we
          // already have and hand it to the server, which puts it in the
          // archive. This is the only moment we know both "it ended" and "we
          // have the data".
          archiveChartIfAvailable(ws._taskId);
        }
        else if (msg.type === "state") {
          if (msg.captures) {
            const st = state.tasks.find((x) => x.task_id === ws._taskId);
            if (st) st.captures = msg.captures;
          }
          if (["finished","failed","interrupted"].includes(msg.status)) refreshTasks();
        }
        else if (msg.type === "error") appendLogLine(`[error] ${msg.message}`);
        else if (msg.type === "replay_meta") {
          // Server tells us it truncated the replay. Insert a visible note
          // at the top of the console so the user knows there are earlier
          // lines in the log file (not lost, just not loaded).
          const where = logFileName(ws._taskId);
          const note = `-- total ${msg.total_lines} lines, showing last ${msg.shown_lines}; full log: ${where} --`;
          const con = $("log-console");
          if (con.textContent.startsWith("Click")) con.textContent = "";
          con.textContent = note + "\n" + con.textContent;
        }
      } catch (_) {}
    };
    ws.onclose = () => {
      if (state.currentWs === ws) state.currentWs = null;
      // B4 fix: try to reconnect if user is still viewing this task
      scheduleReconnect(taskId);
    };
    ws.onerror = () => appendLogLine("[ws] connection error");
  }

  // B4 fix: exponential backoff reconnect (1s, 2s, 3s, 4s, 5s, max 5 attempts)
  function scheduleReconnect(taskId) {
    if (state.shuttingDown) return;
    // User switched to a different task - don't reconnect
    if (state.currentTaskId !== taskId) return;
    // Task is already in a terminal state - no need to reconnect
    const task = state.tasks.find((t) => t.task_id === taskId);
    if (task && ["finished", "failed", "interrupted"].includes(task.status)) return;
    // Already have a reconnect scheduled
    if (state._wsReconnectTimer) return;

    state._wsReconnectAttempts = (state._wsReconnectAttempts || 0) + 1;
    if (state._wsReconnectAttempts > 5) {
      appendLogLine(`[ws] reconnect failed after 5 attempts - please refresh the page`);
      state._wsReconnectAttempts = 0;
      return;
    }
    const delay = Math.min(1000 * state._wsReconnectAttempts, 5000);
    appendLogLine(
      `[ws] disconnected, retrying in ${delay / 1000}s (attempt ${state._wsReconnectAttempts}/5)`
    );
    state._wsReconnectTimer = setTimeout(() => {
      state._wsReconnectTimer = null;
      openWs(taskId);
    }, delay);
  }

  // ---------------- Actions ----------------
  // Run the active script on the currently-selected device. Driven entirely
  // by state (no params) - the script card's Run button calls this with
  // no args. Per-device state.deviceScripts[serial] decides which script runs.
  async function onRunClick() {
    const serial = state.selectedDeviceSerial;
    if (!serial) {
      alert("请先在设备卡上选一台设备");
      return;
    }
    const script = state.deviceScripts[serial];
    if (!script) {
      alert("请先为该设备选择一个脚本");
      return;
    }
    const params = {};
    if (script === "ir_runner.py") {
      const seqName = state.deviceSequences[serial];
      if (!seqName) {
        alert("ir_runner 需要先选序列(点上方 ir_runner 卡片)");
        return;
      }
      params.sequence = `ir_sequences/${seqName}.ini`;
    }
    // Merge per-(device, script) params config (if any). For scripts without
    // config this is a no-op and the script runs with its own defaults.
    const stored = state.deviceParams?.[serial]?.[script];
    if (stored && Object.keys(stored).length) Object.assign(params, stored);
    // Serial capture is opt-in per run. A script that drives the port itself
    // wins by default on the server; the card already disables the control in
    // that case, so this only sends it when it is meaningful.
    const scriptMeta = state.scripts.find((s) => s.filename === script);
    const wantSerial = !!state.deviceSerialCapture[serial] && !(scriptMeta && scriptMeta.owns_serial);
    const serialPort = state.deviceSerialPort[serial] || "";
    if (wantSerial && !serialPort) {
      alert("已勾选串口日志,但未选择 COM 端口");
      return;
    }
    try {
      const res = await api("/api/run", {
        method: "POST",
        body: JSON.stringify({
          device: serial, script, params,
          serial_capture: wantSerial,
          serial_port: wantSerial ? serialPort : null,
          logcat_capture: state.deviceLogcatCapture[serial] !== false,
        }),
      });
      await refreshTasks();
      viewTaskLogs(res.task_id);
    } catch (e) {
      alert(`运行失败: ${e.message}`);
    }
  }

  function onSelectDevice(serial) {
    state.currentDeviceSerial = serial;
    state.selectedDeviceSerial = serial;  // drives script panel highlight
    savePersistedState();
    const running = state.tasks.find(
      (t) => t.device === serial && (t.status === "running" || t.status === "interrupting")
    );
    if (running) {
      viewTaskLogs(running.task_id);
    } else {
      // try the most recent task for this device (any status)
      const last = [...state.tasks]
        .filter((t) => t.device === serial)
        .sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""))[0];
      if (last) {
        viewTaskLogs(last.task_id);
      } else {
        // Device has no tasks at all - close any leftover WS from a previous
        // selection so it doesn't keep pushing lines into this now-empty
        // console. (Defensive pair: WS onmessage also filters by taskId.)
        if (state.currentWs) {
          try { state.currentWs.close(); } catch (_) {}
          state.currentWs = null;
        }
        state.currentTaskId = null;
        setConsoleTarget(null, serial);
        clearConsole();
        syncPerfChart();   // no task -> hide perf chart
      }
    }
    renderDevices();
    renderScripts();  // refresh script panel to reflect newly-selected device
  }

  async function onStopClick() {
    const tid = state.currentTaskId;
    if (!tid) return;
    try {
      await api(`/api/stop/${tid}`, { method: "POST" });
    } catch (e) {
      alert(`中断失败: ${e.message}`);
    }
  }

  async function onForceStopClick(serial) {
    if (!confirm(`设备 ${serial} 临时离线,普通停止可能不响应。\n是否直接杀掉该设备的子进程?\n\n(日志文件会保留)`)) return;
    try {
      const r = await api(`/api/tasks/force-stop-by-device/${encodeURIComponent(serial)}`, { method: "POST" });
      alert(`已硬停 ${r.killed} 个任务`);
      await refreshTasks();
    } catch (e) {
      alert(`硬停失败: ${e.message}`);
    }
  }

  async function onDeleteTask(taskId) {
    const t = state.tasks.find((x) => x.task_id === taskId);
    if (!t) return;
    if (!confirm(
      `删除任务?\n\n设备: ${t.device}\n脚本: ${t.script}\n开始时间: ${fmtTime(t.started_at)}\n\n日志文件会一起删除,无法恢复。`
    )) return;
    try {
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    } catch (e) {
      alert(`删除失败: ${e.message}`);
      return;
    }
    // Drop this task's perf buffer (memory hygiene) + hide chart if it was bound
    delete state.perfSamples[taskId];
    if (state.perfMeta && state.perfMeta.taskId === taskId) state.perfMeta = null;
    if (state.currentTaskId === taskId) {
      if (state.currentWs) { try { state.currentWs.close(); } catch (_) {} }
      state.currentWs = null;
      // B3 fix: setConsoleTarget also calls renderLogInfo to clear stale info
      setConsoleTarget(null, null);
      clearConsole();
      syncPerfChart();
    }
    await refreshTasks();
  }

  async function onCleanupTasks() {
    const terminal = state.tasks.filter((t) =>
      ["finished", "failed", "interrupted"].includes(t.status)
    );
    if (terminal.length === 0) {
      alert("没有可清理的已完成任务");
      return;
    }
    if (!confirm(
      `确认清空 ${terminal.length} 个已结束任务?\n\n(包含 ${state.tasks.length - terminal.length} 个正在运行的任务不会被影响)\n\n所有日志文件会一起删除,无法恢复。`
    )) return;
    try {
      const r = await api("/api/tasks/cleanup", { method: "POST" });
      // Drop perf buffers of cleaned tasks (memory hygiene)
      if (r.ids) for (const id of r.ids) delete state.perfSamples[id];
      // If we were viewing one of the deleted tasks, reset
      if (state.currentTaskId && r.ids && r.ids.includes(state.currentTaskId)) {
        if (state.currentWs) { try { state.currentWs.close(); } catch (_) {} }
        state.currentWs = null;
        state.currentTaskId = null;
        if (state.perfMeta) state.perfMeta = null;
        clearConsole();
        syncPerfChart();
      }
      await refreshTasks();
    } catch (e) {
      alert(`清空失败: ${e.message}`);
    }
  }

  // ---------------- Polling ----------------
  // COM port list for the serial-capture opt-in. Cheap, and fetched with the
  // same cadence as scripts rather than the 3s device poll - ports do not
  // appear and vanish often, and a hot-plug is covered by the manual refresh.
  async function refreshSerialPorts() {
    try {
      const r = await api("/api/serial/ports");
      const ports = r.ports || [];
      if (JSON.stringify(ports) === JSON.stringify(state.serialPorts)) return;
      state.serialPorts = ports;
      renderDevices();
    } catch (_) {
      // Server without pyserial: leave the list empty, the control stays
      // disabled and the run still works. Never surface this as an error.
    }
  }

  async function refreshDevices() {
    try {
      const r = await api("/api/devices");
      state.devices = r.devices || [];
      state.backendOnline = true;
      $("backend-pill").classList.remove("off");
    } catch (_) {
      state.backendOnline = false;
      $("backend-pill").classList.add("off");
      return;
    }
    // Cleanup: forget remembered scripts AND per-device sequences for devices
    // that truly disconnected. Don't drop the selection for devices with a
    // running task — they might be temporarily offline.
    const validSerials = new Set(state.devices.map((d) => d.serial));
    const runningSerials = new Set(
      state.tasks
        .filter((t) => t.status === "running" || t.status === "interrupting")
        .map((t) => t.device)
    );
    // Also keep remembered selections for devices whose battery test ended with
    // the device powered off - their card stays visible as a "discharge
    // power-off" card, so dropping the script chip would look like a reset.
    const keptSerials = new Set(runningSerials);
    for (const t of state.tasks) {
      if (t.script !== "battery_inout_stress.py") continue;
      if (!["finished", "interrupted", "failed"].includes(t.status)) continue;
      if (validSerials.has(t.device)) continue;
      const latest = latestTaskOf(t.device);
      if (latest && latest.script === "battery_inout_stress.py") keptSerials.add(t.device);
    }
    let dirty = false;
    for (const serial of Object.keys(state.deviceScripts)) {
      if (!validSerials.has(serial) && !keptSerials.has(serial)) {
        delete state.deviceScripts[serial];
        dirty = true;
      }
    }
    for (const serial of Object.keys(state.deviceSequences)) {
      if (!validSerials.has(serial) && !keptSerials.has(serial)) {
        delete state.deviceSequences[serial];
        dirty = true;
      }
    }
    const k = devicesRenderKey();
    if (k !== _lastKey.devices || dirty) {
      _lastKey.devices = k;
      renderDevices();
    }
  }
  async function refreshScripts() {
    try {
      const r = await api("/api/scripts");
      state.scripts = r.scripts || [];
    } catch (_) { return; }
    // Cleanup: forget remembered scripts for filenames that no longer exist on disk
    const validFilenames = new Set(state.scripts.map((s) => s.filename));
    let dirty = false;
    for (const serial of Object.keys(state.deviceScripts)) {
      if (!validFilenames.has(state.deviceScripts[serial])) {
        delete state.deviceScripts[serial];
        dirty = true;
      }
    }
    // Forget params configs for scripts that no longer exist on disk
    for (const serial of Object.keys(state.deviceParams)) {
      for (const sname of Object.keys(state.deviceParams[serial])) {
        if (!validFilenames.has(sname)) {
          delete state.deviceParams[serial][sname];
          dirty = true;
        }
      }
      if (!Object.keys(state.deviceParams[serial]).length) {
        delete state.deviceParams[serial];
      }
    }
    const k = scriptsRenderKey();
    if (k !== _lastKey.scripts) {
      _lastKey.scripts = k;
      renderScripts();
    }
    // scripts list change also affects device run-button labels / dropdown options, so re-render devices
    const dk = devicesRenderKey();
    if (dk !== _lastKey.devices || dirty) {
      _lastKey.devices = dk;
      renderDevices();
    }
  }
  async function refreshTasks() {
    try {
      const r = await api("/api/tasks");
      state.tasks = r.tasks || [];
    } catch (_) { return; }
    const tk = tasksRenderKey();
    if (tk !== _lastKey.tasks) {
      _lastKey.tasks = tk;
      renderTasks();
    }
    // task changes always affect device cards (running badges)
    const dk = devicesRenderKey();
    if (dk !== _lastKey.devices) {
      _lastKey.devices = dk;
      renderDevices();
    }
    // also re-evaluate stop button enabled state
    if (state.currentTaskId) {
      const cur = state.tasks.find((t) => t.task_id === state.currentTaskId);
      $("btn-stop-task").disabled = !cur || !["running","interrupting"].includes(cur.status);
    }
    // B2 fix: refresh log info bar so its status badge / runtime stays in sync
    // with the underlying task (e.g., running -> finished transition)
    if (state.currentTaskId) {
      renderLogInfo();
    }
    // Backfill charts for tasks that finished while we were NOT watching them.
    // The WS "end" frame is filtered out when currentTaskId is a different task,
    // and a page reload loses state.perfSamples entirely (WS replay rebuilds it
    // from the archived log). This sweep catches both: any archived task whose
    // samples we hold but whose archive has no chart.png gets one upload.
    // archiveChartIfAvailable() claims the id before awaiting, so this cannot
    // loop or upload twice.
    for (const t of state.tasks) {
      if (t.archive && !state.chartPosted[t.task_id]
          && (state.perfSamples[t.task_id] || []).length) {
        archiveChartIfAvailable(t.task_id);
      }
    }
  }

  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString();
    updateLiveDurations();
  }

  // B1 fix: live duration ticker.
  // Direct DOM update - no full re-render, no flicker, no animation reset.
  function updateLiveDurations() {
    state.tasks.forEach((t) => {
      if (!["running", "interrupting"].includes(t.status)) return;
      const card = document.querySelector(`.task-card[data-task="${t.task_id}"]`);
      if (!card) return;
      const durEl = card.querySelector(".task-duration");
      if (durEl) durEl.textContent = fmtDuration(t.started_at, null);
    });
    if (state.currentTaskId) {
      const t = state.tasks.find((x) => x.task_id === state.currentTaskId);
      if (t && ["running", "interrupting"].includes(t.status)) {
        const rtEl = $("log-info").querySelector(".runtime");
        if (rtEl) rtEl.textContent = fmtDuration(t.started_at, null);
      }
    }
  }

  // ---------------- Server status ----------------
  function fmtUptime(sec) {
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m`;
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return `${h}h${m}m`;
  }

  async function refreshServerStatus() {
    if (state.shuttingDown) return;
    try {
      const s = await api("/api/server/status");
      state.server = s;
      renderServerInfo();
    } catch (_) {}
    refreshArchiveStats();
  }

  function renderServerInfo() {
    const el = $("server-info");
    if (!state.server) {
      el.innerHTML = "服务不可达";
      return;
    }
    const s = state.server;
    const taskBadge = s.running_tasks > 0
      ? `<span class="warn">${s.running_tasks} 任务运行中</span>`
      : `<span class="hi">空闲</span>`;
    el.innerHTML = `PID <span class="hi">${s.pid}</span> · 运行 ${fmtUptime(s.uptime_sec)} · ${taskBadge}`;
  }

  async function onShutdownClick() {
    if (state.shuttingDown) return;
    const running = state.server?.running_tasks || 0;
    const msg = running > 0
      ? `确定关闭 PPTP 吗?\n\n当前有 ${running} 个任务正在运行,会被中断。\n关闭后刷新页面会失败。`
      : `确定关闭 PPTP 服务吗?`;
    if (!confirm(msg)) return;

    state.shuttingDown = true;
    const btn = $("btn-shutdown");
    btn.disabled = true;
    btn.textContent = "关闭中…";

    // close WS first (otherwise the server will reject new connections after shutdown)
    if (state.currentWs) {
      try { state.currentWs.close(); } catch (_) {}
      state.currentWs = null;
    }
    if (state._wsReconnectTimer) {
      clearTimeout(state._wsReconnectTimer);
      state._wsReconnectTimer = null;
    }
    // disable polling
    clearInterval(state._pollDevices);
    clearInterval(state._pollTasks);
    clearInterval(state._pollServer);
    clearInterval(state._pollClock);

    try {
      await api("/api/server/shutdown", { method: "POST" });
    } catch (_) {
      // expected - server closes connection during shutdown
    }

    // show shutdown screen
    document.body.innerHTML = `
      <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;
                  height:100vh;background:#0f1419;color:#e4e7eb;font-family:sans-serif;">
        <h2 style="margin:0 0 12px;color:#4ade80;">PPTP 已关闭</h2>
        <p style="color:#8a93a3;margin:0;">服务进程已退出。可以关闭浏览器窗口。</p>
        <p style="color:#8a93a3;margin:24px 0 0;font-size:12px;">
          重新启动:双击 <code style="background:#1a1f29;padding:2px 6px;border-radius:3px;">start.bat</code>
        </p>
      </div>`;
  }

  // ---------------- Init ----------------
  function bindEvents() {
    $("btn-refresh-devices").addEventListener("click", refreshDevices);
    $("btn-refresh-scripts").addEventListener("click", refreshScripts);
    $("btn-refresh-tasks").addEventListener("click", refreshTasks);
    $("btn-cleanup-tasks").addEventListener("click", onCleanupTasks);
    $("btn-clear-log").addEventListener("click", clearConsole);
    $("btn-export-log").addEventListener("click", onExportLog);
    $("btn-stop-task").addEventListener("click", onStopClick);
    $("btn-hard-stop").addEventListener("click", onForceStopCurrent);
    $("btn-shutdown").addEventListener("click", onShutdownClick);
    $("btn-reset").addEventListener("click", onResetClick);
    $("btn-open-archive").addEventListener("click", onRevealArchiveRoot);

    // Modal: IR sequence picker (list + select)
    document.querySelectorAll("#modal-seq [data-close]").forEach((b) => {
      b.addEventListener("click", closeSeqModal);
    });
    // Click on backdrop (outside modal) closes
    $("modal-seq").addEventListener("click", (e) => {
      if (e.target.id === "modal-seq") closeSeqModal();
    });
    // ESC to close
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("modal-seq").hidden) closeSeqModal();
    });

    // Modal: script params config
    document.querySelectorAll("#modal-params [data-close]").forEach((b) => {
      b.addEventListener("click", closeParamsModal);
    });
    $("modal-params").addEventListener("click", (e) => {
      if (e.target.id === "modal-params") closeParamsModal();
    });
    $("btn-params-save").addEventListener("click", saveParams);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("modal-params").hidden) closeParamsModal();
    });

    // console scroll detection: pause auto-scroll when user scrolls up
    const con = $("log-console");
    con.addEventListener("scroll", () => {
      const atBottom = con.scrollHeight - con.scrollTop - con.clientHeight < 20;
      state.userScrolledUp = !atBottom;
    });

    // Keep the perf chart sized to the log panel when the window resizes
    window.addEventListener("resize", () => {
      if (perfChart) perfChart.resize();
    });
  }

  // ---------------- IR sequence picker modal ----------------
  // Sequences are .ini files in ir_sequences/. The modal is a pure picker:
  // - Lists existing sequences
  // - Allows creating a new one (with a minimal template)
  // - Selecting one binds it to a device (state.deviceSequences[device] = filename)
  // No content editing - the user edits the .ini file directly.
  let _seqListCache = null;  // [{name, filename, mtime, size}, ...]

  async function loadSeqList() {
    if (_seqListCache) return _seqListCache;
    try {
      const r = await api("/api/sequences");
      _seqListCache = r.sequences || [];
    } catch (_) {
      _seqListCache = [];
    }
    return _seqListCache;
  }

  function invalidateSeqList() { _seqListCache = null; }

  // State context for the current modal session:
  //   { target: "device" | "file", device: serial | null, file: "default" }
  // The picker selects a sequence file (filename stem) and binds it to the
  // target. For device target, the selection is stored in
  // state.deviceSequences[device]. For file target, the modal just closes.
  let _seqPickerContext = null;
  let _seqSelected = null;  // filename stem currently being picked

  async function openSeqModal(opts) {
    // Back-compat: openSeqModal("default") or openSeqModal({device})
    let ctx = { target: "file", device: null, file: "default" };
    if (typeof opts === "string") {
      ctx.file = opts || "default";
    } else if (opts && typeof opts === "object") {
      if (opts.device) {
        ctx.target = "device";
        ctx.device = opts.device;
      } else if (opts.file) {
        ctx.file = opts.file;
      }
    }
    _seqPickerContext = ctx;
    _seqSelected = null;

    // Show the target device in the modal header
    $("seq-target").textContent = ctx.target === "device"
      ? `为设备 ${ctx.device} 选择序列`
      : "选择序列文件";

    $("seq-status").textContent = "";
    $("modal-seq").hidden = false;

    await renderSeqList();
  }

  function closeSeqModal() {
    $("modal-seq").hidden = true;
  }

  async function renderSeqList() {
    const list = $("seq-list");
    const items = await loadSeqList();
    $("seq-count").textContent = `(${items.length} 个)`;
    if (!items.length) {
      list.innerHTML = `<div class="seq-list-empty">暂无序列文件<br>
        <small>在下方输入名称,点击"+ 新建"创建模板</small></div>`;
      return;
    }
    const target = _seqPickerContext;
    // Pre-select the device's currently-bound sequence (if device mode)
    let preselect = null;
    if (target && target.target === "device" && target.device) {
      preselect = state.deviceSequences[target.device] || null;
    } else if (target && target.target === "file") {
      preselect = target.file || null;
    }
    _seqSelected = preselect;

    list.innerHTML = items.map((it) => {
      const isDefault = it.name === "default";
      const selected = it.name === _seqSelected;
      const radio = selected ? "●" : "○";
      return `
        <div class="seq-list-item ${selected ? "selected" : ""}" data-name="${esc(it.name)}">
          <span class="seq-name">${esc(it.name)}.ini</span>
          ${isDefault ? "" : `<button class="seq-del" data-del="${esc(it.name)}" title="删除此序列(临时)">×</button>`}
          <span class="seq-radio">${radio}</span>
        </div>`;
    }).join("");

    // Click row → select sequence (commit immediately, no Save button)
    list.querySelectorAll(".seq-list-item").forEach((row) => {
      row.addEventListener("click", (e) => {
        if (e.target.closest(".seq-del")) return;
        selectSequence(row.dataset.name);
      });
    });
    // Click × → delete sequence (with confirm)
    list.querySelectorAll(".seq-del").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const name = btn.dataset.del;
        if (!confirm(`删除序列 ${name}.ini?\n\n(不会删除 default.ini)`)) return;
        try {
          await api(`/api/sequences/${encodeURIComponent(name)}`, { method: "DELETE" });
          invalidateSeqList();
          await renderSeqList();
        } catch (err) {
          alert(`删除失败: ${err.message}`);
        }
      });
    });
  }

  // User picked a sequence. Persist binding to state and close.
  function selectSequence(name) {
    const ctx = _seqPickerContext;
    if (ctx.target === "device" && ctx.device) {
      state.deviceSequences[ctx.device] = name;
      savePersistedState();
      renderDevices();
      // Also re-render the script panel so the bound sequence shows in
      // the ir_runner card subtitle (and the Run button becomes enabled).
      renderScripts();
      $("seq-status").textContent = `✓ 设备 ${ctx.device} 已绑定 ${name}.ini`;
    } else {
      $("seq-status").textContent = `✓ 选中 ${name}.ini(仅查看,未绑定设备)`;
    }
    setTimeout(closeSeqModal, 400);
  }

  // ---------------- Script params config modal ----------------
  // Scripts that declare params support a `--dump-params` flag which prints
  // {"fields": [...]}. The platform renders a modal from this schema and
  // stores the values per (device, script) in state.deviceParams (persisted
  // like sequences, strictly independent per pair).
  let _paramsSchemaCache = {};   // { [scriptName]: fields[] }
  let _paramsCtx = null;         // { device, script }
  let _paramsFields = [];

  async function loadParamsSchema(name) {
    if (_paramsSchemaCache[name]) return _paramsSchemaCache[name];
    try {
      const r = await api(`/api/scripts/${encodeURIComponent(name)}/params`);
      _paramsSchemaCache[name] = r.fields || [];
    } catch (_) {
      _paramsSchemaCache[name] = [];
    }
    return _paramsSchemaCache[name];
  }

  function paramInputHtml(f, value) {
    const n = esc(f.name);
    if (f.type === "bool") {
      return `<input type="checkbox" name="${n}" ${value ? "checked" : ""}>`;
    }
    if (f.type === "select") {
      // Dropdown; choices may be plain strings or {value, label} objects.
      const opts = (f.choices || []).map((c) => {
        const o = (c && typeof c === "object") ? c : { value: c, label: c };
        const sel = String(o.value) === String(value) ? " selected" : "";
        return `<option value="${esc(o.value)}"${sel}>${esc(o.label != null ? o.label : o.value)}</option>`;
      }).join("");
      return `<select name="${n}">${opts}</select>`;
    }
    if (f.type === "multiselect") {
      // A checkbox GROUP sharing one name, so the selection is one param whose
      // value is an array. Anything that is not an array - a comma string from a
      // hand-written --params, a single value, undefined - is normalised here,
      // because the same value round-trips through localStorage and the CLI.
      const cur = Array.isArray(value) ? value.map(String)
        : (value == null || value === "" ? [] : String(value).split(","));
      const boxes = (f.choices || []).map((c) => {
        const o = (c && typeof c === "object") ? c : { value: c, label: c };
        const on = cur.includes(String(o.value)) ? " checked" : "";
        // `data-multi` is what saveParams matches on. It is not decoration: the
        // whole group shares a name, so the selector that reads it back has to
        // be able to name the group and not the single first box.
        return `<label class="param-multi-item"><input type="checkbox" name="${n}"`
             + ` data-multi="1" value="${esc(o.value)}"${on}>`
             + `<span>${esc(o.label != null ? o.label : o.value)}</span></label>`;
      }).join("");
      return `<div class="param-multi">${boxes}</div>`;
    }
    if (f.type === "int" || f.type === "float") {
      let extra = "";
      if (f.min != null) extra += ` min="${f.min}"`;
      if (f.max != null) extra += ` max="${f.max}"`;
      const step = f.type === "float" ? "any" : "1";
      return `<input type="number" name="${n}" step="${step}"${extra} value="${esc(value)}">`;
    }
    return `<input type="text" name="${n}" value="${esc(value)}">`;
  }

  async function openParamsModal({ device, script }) {
    _paramsCtx = { device, script };
    _paramsFields = await loadParamsSchema(script);
    if (!_paramsFields.length) {
      $("params-status").textContent = "该脚本未声明参数";
      return;
    }
    $("params-target").textContent = `设备 ${device} · ${script}`;
    $("params-form").innerHTML = _paramsFields.map((f) => {
      const cur = state.deviceParams?.[device]?.[script]?.[f.name] ?? f.default;
      return `
        <div class="param-row">
          <label class="param-label">${esc(f.label || f.name)}</label>
          <div class="param-control">${paramInputHtml(f, cur)}</div>
        </div>`;
    }).join("");
    $("params-status").textContent = "";
    $("modal-params").hidden = false;
  }

  function saveParams() {
    const ctx = _paramsCtx;
    if (!ctx) return;
    const params = {};
    for (const f of _paramsFields) {
      // A multiselect is many boxes under ONE name, so it must not go through
      // querySelector: that returns only the first match, and the selection
      // would silently collapse to whichever box happens to come first in the
      // DOM. An empty selection is a real answer, not a missing one - the
      // script keeps the locked FAST trio and runs with the rest deselected.
      if (f.type === "multiselect") {
        const boxes = document.querySelectorAll(
          `#modal-params [name="${esc(f.name)}"][data-multi="1"]`);
        params[f.name] = Array.from(boxes).filter((b) => b.checked)
          .map((b) => b.value);
        continue;
      }
      const el = document.querySelector(`#modal-params [name="${esc(f.name)}"]`);
      if (!el) continue;
      if (f.type === "bool") params[f.name] = el.checked;
      else if (f.type === "int") params[f.name] = parseInt(el.value, 10) || 0;
      else if (f.type === "float") params[f.name] = parseFloat(el.value) || 0;
      else params[f.name] = el.value;
    }
    if (!state.deviceParams[ctx.device]) state.deviceParams[ctx.device] = {};
    state.deviceParams[ctx.device][ctx.script] = params;
    savePersistedState();
    renderScripts();
    $("params-status").textContent = `✓ 已保存 · 设备 ${ctx.device} · ${ctx.script}`;
    setTimeout(closeParamsModal, 400);
  }

  function closeParamsModal() {
    $("modal-params").hidden = true;
  }

  // Force-stop the currently-viewed task. Used when device is temp-offline
  // (normal "中断" sends CTRL_BREAK which may not reach the subprocess).
  async function onForceStopCurrent() {
    const tid = state.currentTaskId;
    const t = state.tasks.find((x) => x.task_id === tid);
    if (!t) return;
    if (!confirm(`设备 ${t.device} 临时离线,普通停止可能不响应。\n是否直接杀掉子进程?\n\n(日志文件会保留)`)) return;
    try {
      const r = await api(`/api/tasks/force-stop-by-device/${encodeURIComponent(t.device)}`, { method: "POST" });
      alert(`已硬停 ${r.killed} 个任务`);
      await refreshTasks();
    } catch (e) {
      alert(`硬停失败: ${e.message}`);
    }
  }

  async function onResetClick() {
    if (!confirm("硬重置:\n1) 杀掉所有正在运行的任务\n2) 清空任务列表\n3) 重启 ADB server\n\n确定继续?")) return;
    const btn = $("btn-reset");
    btn.disabled = true;
    btn.textContent = "重置中…";
    try {
      await api("/api/tasks/force-cleanup", { method: "POST" });
    } catch (e) {
      // 继续尝试 reconnect adb
      console.warn("force-cleanup failed:", e);
    }
    try {
      await api("/api/adb/reconnect", { method: "POST" });
    } catch (e) {
      console.warn("adb reconnect failed:", e);
    }
    // Reset local view
    if (state.currentWs) { try { state.currentWs.close(); } catch (_) {} }
    state.currentWs = null;
    state.currentTaskId = null;
    state.currentDeviceSerial = null;
    state.perfMeta = null;
    state.perfSamples = {};   // reset wipes all perf buffers
    setConsoleTarget(null, null);
    clearConsole();
    syncPerfChart();
    await refreshTasks();
    await refreshDevices();
    btn.disabled = false;
    btn.textContent = "重置";
  }

  // ===== localStorage persistence =====
  // What we keep across page refresh / server restart:
  //   - selectedDeviceSerial  (user preference: which device they're looking at)
  //   - deviceSequences       (user config: which device runs which IR sequence)
  //   - deviceParams          (user config: per-device, per-script script params)
  //
  // What we DELIBERATELY discard (2026-07-20 reversal):
  //   - deviceScripts         (session state: "what do I want to run RIGHT NOW";
  //                            re-selecting from the dropdown is cheap and the
  //                            previous design that persisted this confused users
  //                            on restart - cards showed a script chosen from
  //                            a previous session that may no longer be valid).
  //
  // Server-side data (devices, scripts, tasks, server info) is always
  // re-fetched from the backend on init, so we only persist user choices.
  const STORAGE_KEY = "pptp.deviceState.v1";

  function loadPersistedState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (data.selectedDeviceSerial)
        state.selectedDeviceSerial = data.selectedDeviceSerial;
      // deviceScripts intentionally NOT loaded - see comment above.
      if (data.deviceSequences && typeof data.deviceSequences === "object")
        state.deviceSequences = data.deviceSequences;
      if (data.deviceParams && typeof data.deviceParams === "object")
        state.deviceParams = data.deviceParams;
      // Serial port is a per-device preference worth remembering. The checkbox
      // itself is NOT persisted - same reasoning as deviceScripts: "what do I
      // want to capture RIGHT NOW" is session state, and a stale opt-in
      // silently holding a COM port on the next session would be surprising.
      if (data.deviceSerialPort && typeof data.deviceSerialPort === "object")
        state.deviceSerialPort = data.deviceSerialPort;
    } catch (_) {
      // Ignore corrupt state - just start fresh
    }
  }

  let _saveTimer = null;
  function savePersistedState() {
    // Debounce: batch rapid state changes into one write
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
      try {
        const data = {
          selectedDeviceSerial: state.selectedDeviceSerial,
          // deviceScripts intentionally NOT saved - see comment above.
          deviceSequences: state.deviceSequences,
          deviceParams: state.deviceParams,
          // deviceSerialCapture intentionally NOT saved - see loadPersistedState.
          deviceSerialPort: state.deviceSerialPort,
        };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
      } catch (_) { /* quota exceeded etc - ignore */ }
    }, 200);
  }

  function init() {
    loadPersistedState();
    bindEvents();
    refreshDevices();
    refreshScripts();
    refreshSerialPorts();
    refreshTasks();
    refreshServerStatus();
    state._pollDevices = setInterval(refreshDevices, 3000);
    state._pollTasks = setInterval(refreshTasks, 2000);
    state._pollServer = setInterval(refreshServerStatus, 5000);
    state._pollClock = setInterval(tickClock, 1000);
    tickClock();
  }

  document.addEventListener("DOMContentLoaded", init);
})();