/* ── State ── */
let config = {};
let currentButton = null;

const overlay = document.getElementById("modal-overlay");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const tabs = document.querySelectorAll(".modal__tab");
const panelPress = document.getElementById("panel-press");
const panelLongpress = document.getElementById("panel-longpress");
const actionsPress = document.getElementById("actions-press");
const actionsLongpress = document.getElementById("actions-longpress");
const longpressSeconds = document.getElementById("longpress-seconds");

/* ── Load config ── */
async function loadConfig() {
  const res = await fetch("/api/config");
  config = await res.json();
  updateIndicators();
}

/* ── Save full config ── */
async function saveConfig() {
  await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

/* ── Update button indicators ── */
function updateIndicators() {
  document.querySelectorAll(".btn").forEach((btn) => {
    const key = btn.dataset.key;
    const bc = config[key] || {};
    const hasPress = Array.isArray(bc.press) && bc.press.length > 0;
    const hasLong = !!(bc.longpress && Array.isArray(bc.longpress.actions) && bc.longpress.actions.length > 0);
    btn.classList.toggle("has-press", hasPress);
    btn.classList.toggle("has-longpress", hasLong);
  });
}

/* ── Create action row HTML ── */
function createActionRow(container, action = { type: "get", url: "" }) {
  const row = document.createElement("div");
  row.className = "action-row";

  const num = document.createElement("span");
  num.className = "action-row__num";
  row.appendChild(num);

  // Type selector
  const typeWrap = document.createElement("div");
  typeWrap.className = "action-row__type";
  const select = document.createElement("select");
  ["get", "bash", "pause"].forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t === "get" ? "GET" : t === "bash" ? "Bash" : "Пауза";
    select.appendChild(opt);
  });
  select.value = action.type || "get";
  typeWrap.appendChild(select);
  row.appendChild(typeWrap);

  // Input
  const inputWrap = document.createElement("div");
  inputWrap.className = "action-row__input";
  const input = document.createElement("input");
  updateInput(input, select.value, action);
  inputWrap.appendChild(input);
  row.appendChild(inputWrap);

  select.addEventListener("change", () => {
    updateInput(input, select.value, {});
  });

  // Controls
  const controls = document.createElement("div");
  controls.className = "action-row__controls";

  const btnUp = document.createElement("button");
  btnUp.textContent = "\u2191";
  btnUp.title = "Вверх";
  btnUp.addEventListener("click", () => {
    const prev = row.previousElementSibling;
    if (prev) {
      container.insertBefore(row, prev);
      renumber(container);
    }
  });

  const btnDown = document.createElement("button");
  btnDown.textContent = "\u2193";
  btnDown.title = "Вниз";
  btnDown.addEventListener("click", () => {
    const next = row.nextElementSibling;
    if (next) {
      container.insertBefore(next, row);
      renumber(container);
    }
  });

  const btnDel = document.createElement("button");
  btnDel.className = "action-delete";
  btnDel.textContent = "\u00D7";
  btnDel.title = "Удалить";
  btnDel.addEventListener("click", () => {
    row.remove();
    renumber(container);
  });

  controls.appendChild(btnUp);
  controls.appendChild(btnDown);
  controls.appendChild(btnDel);
  row.appendChild(controls);

  container.appendChild(row);
  renumber(container);
  return row;
}

function updateInput(input, type, action) {
  if (type === "get") {
    input.type = "url";
    input.placeholder = "https://example.com/api/action";
    input.value = action.url || "";
  } else if (type === "bash") {
    input.type = "text";
    input.placeholder = "systemctl restart myservice";
    input.value = action.command || "";
  } else {
    input.type = "number";
    input.placeholder = "секунды";
    input.min = "0.1";
    input.step = "0.1";
    input.value = action.seconds || "";
  }
}

function renumber(container) {
  container.querySelectorAll(".action-row").forEach((row, i) => {
    row.querySelector(".action-row__num").textContent = i + 1;
  });
}

/* ── Read actions from DOM ── */
function readActions(container) {
  const actions = [];
  container.querySelectorAll(".action-row").forEach((row) => {
    const type = row.querySelector("select").value;
    const input = row.querySelector(".action-row__input input");
    const val = input.value.trim();
    if (!val) return;
    if (type === "get") actions.push({ type: "get", url: val });
    else if (type === "bash") actions.push({ type: "bash", command: val });
    else actions.push({ type: "pause", seconds: parseFloat(val) });
  });
  return actions;
}

/* ── Populate actions into DOM ── */
function populateActions(container, actions) {
  container.innerHTML = "";
  (actions || []).forEach((a) => createActionRow(container, a));
}

/* ── Open modal ── */
function openModal(key) {
  currentButton = key;
  const label = key.toUpperCase();
  modalTitle.textContent = label;

  const bc = config[key] || {};

  // Populate press actions
  populateActions(actionsPress, bc.press || []);

  // Populate longpress
  const lp = bc.longpress || {};
  longpressSeconds.value = lp.seconds || 3;
  populateActions(actionsLongpress, lp.actions || []);

  // Reset to press tab
  switchTab("press");

  overlay.classList.add("open");
}

/* ── Close modal ── */
function closeModal() {
  overlay.classList.remove("open");
  currentButton = null;
}

/* ── Switch tab ── */
function switchTab(tab) {
  tabs.forEach((t) => {
    t.classList.toggle("modal__tab--active", t.dataset.tab === tab);
  });
  panelPress.classList.toggle("modal__panel--hidden", tab !== "press");
  panelLongpress.classList.toggle("modal__panel--hidden", tab !== "longpress");
}

/* ── Save current button ── */
function saveCurrentButton() {
  if (!currentButton) return;

  const pressActions = readActions(actionsPress);
  const longpressActions = readActions(actionsLongpress);
  const lpSeconds = parseFloat(longpressSeconds.value) || 3;

  const bc = {};
  if (pressActions.length > 0) bc.press = pressActions;
  if (longpressActions.length > 0) {
    bc.longpress = { seconds: lpSeconds, actions: longpressActions };
  }

  if (Object.keys(bc).length > 0) {
    config[currentButton] = bc;
  } else {
    delete config[currentButton];
  }

  saveConfig();
  updateIndicators();
  closeModal();
}

/* ── Event listeners ── */

// Button clicks (skip settings button which has no data-key)
document.querySelectorAll(".btn[data-key]").forEach((btn) => {
  btn.addEventListener("click", () => openModal(btn.dataset.key));
});

// Tabs
tabs.forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

// Add action buttons
document.getElementById("add-press").addEventListener("click", () => {
  createActionRow(actionsPress);
});
document.getElementById("add-longpress").addEventListener("click", () => {
  createActionRow(actionsLongpress);
});

// Modal buttons
document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal-cancel").addEventListener("click", closeModal);
document.getElementById("modal-save").addEventListener("click", saveCurrentButton);

// Close on overlay click
overlay.addEventListener("click", (e) => {
  if (e.target === overlay) closeModal();
});

// Close on Escape
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeModal();
    closeSettings();
    closeLog();
  }
});

/* ── Voice Log modal ── */
const logOverlay = document.getElementById("log-overlay");
const logList = document.getElementById("voice-log-list");
let logPollTimer = null;

function openLog() {
  logOverlay.classList.add("open");
  loadVoiceLog();
  logPollTimer = setInterval(loadVoiceLog, 3000);
}

function closeLog() {
  logOverlay.classList.remove("open");
  if (logPollTimer) {
    clearInterval(logPollTimer);
    logPollTimer = null;
  }
}

async function loadVoiceLog() {
  try {
    const res = await fetch("/api/voice-log");
    const logs = await res.json();
    renderVoiceLog(logs);
  } catch (e) {
    // ignore fetch errors
  }
}

function renderVoiceLog(logs) {
  if (!logs.length) {
    logList.innerHTML = '<div class="voice-log__empty">No voice commands yet</div>';
    return;
  }
  // Render newest first
  logList.innerHTML = logs
    .slice()
    .reverse()
    .map((entry) => {
      const date = new Date(entry.ts * 1000);
      const time = date.toLocaleTimeString();
      const steps = (entry.steps || [])
        .map((s) => {
          let label = s.step;
          let cls = "voice-log__step-tag";
          if (s.step === "transcribe" && s.detail !== "starting...") label = "speech";
          else if (s.step === "llm_call") label = "LLM";
          else if (s.step === "llm_result") label = "AI response";
          else if (s.step === "execute") { label = "execute"; cls += " voice-log__step-tag--ok"; }
          else if (s.step === "done") { label = "done"; cls += " voice-log__step-tag--ok"; }
          else if (s.step === "error") { label = "error"; cls += " voice-log__step-tag--err"; }
          else if (s.step === "unknown") { label = "no match"; cls += " voice-log__step-tag--warn"; }
          return `<div class="voice-log__step"><span class="${cls}">${label}</span><span class="voice-log__step-detail">${escapeHtml(s.detail)}</span></div>`;
        })
        .join("");
      return `<div class="voice-log__entry"><div class="voice-log__time">${time}</div><div class="voice-log__steps">${steps}</div></div>`;
    })
    .join("");
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

document.getElementById("btn-voice-log").addEventListener("click", (e) => {
  e.stopPropagation();
  openLog();
});
document.getElementById("log-close").addEventListener("click", closeLog);
logOverlay.addEventListener("click", (e) => {
  if (e.target === logOverlay) closeLog();
});

/* ── Settings modal ── */
let voiceConfig = {};
const settingsOverlay = document.getElementById("settings-overlay");
const voiceCommandsList = document.getElementById("voice-commands-list");

async function loadVoiceConfig() {
  const res = await fetch("/api/voice-config");
  voiceConfig = await res.json();
}

function openSettings() {
  loadVoiceConfig().then(() => {
    populateSettings();
    settingsOverlay.classList.add("open");
  });
}

function closeSettings() {
  settingsOverlay.classList.remove("open");
}

function populateSettings() {
  document.getElementById("voice-enabled").checked = voiceConfig.enabled || false;

  document.querySelectorAll('input[name="provider"]').forEach((r) => {
    r.checked = r.value === (voiceConfig.provider || "claude");
  });

  document.getElementById("claude-api-key").value = voiceConfig.claude_api_key || "";
  document.getElementById("claude-model").value = voiceConfig.claude_model || "claude-haiku-4-5-20251001";
  document.getElementById("openai-api-key").value = voiceConfig.openai_api_key || "";
  document.getElementById("openai-model").value = voiceConfig.openai_model || "gpt-4o-mini";
  document.getElementById("whisper-model").value = voiceConfig.whisper_model || "small";
  document.getElementById("voice-language").value = voiceConfig.language || "en";
  document.getElementById("audio-device").value = voiceConfig.audio_device || "plughw:0,0";

  // Populate commands
  voiceCommandsList.innerHTML = "";
  (voiceConfig.commands || []).forEach((cmd) => createVoiceCommandRow(cmd));
}

function createVoiceCommandRow(cmd = { name: "", description: "", url: "" }) {
  const row = document.createElement("div");
  row.className = "voice-cmd-row";

  // Row 1: name + url + delete
  const top = document.createElement("div");
  top.className = "voice-cmd__top";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "music";
  nameInput.value = cmd.name || "";
  nameInput.className = "voice-cmd-name";

  const urlInput = document.createElement("input");
  urlInput.type = "url";
  urlInput.placeholder = "http://192.168.1.95:8241/api/...";
  urlInput.value = cmd.url || "";
  urlInput.className = "voice-cmd-url";

  const btnDel = document.createElement("button");
  btnDel.className = "action-delete";
  btnDel.textContent = "\u00D7";
  btnDel.title = "Delete";
  btnDel.addEventListener("click", () => row.remove());

  top.appendChild(nameInput);
  top.appendChild(urlInput);
  top.appendChild(btnDel);
  row.appendChild(top);

  // Row 2: keywords
  const descInput = document.createElement("textarea");
  descInput.placeholder = "muzică, музыка, play music, pune ceva, dance";
  descInput.value = cmd.description || "";
  descInput.className = "voice-cmd-desc";
  descInput.rows = 2;
  row.appendChild(descInput);

  voiceCommandsList.appendChild(row);
}

function readVoiceConfig() {
  const provider = document.querySelector('input[name="provider"]:checked');
  const commands = [];
  voiceCommandsList.querySelectorAll(".voice-cmd-row").forEach((row) => {
    const name = row.querySelector(".voice-cmd-name").value.trim();
    const description = row.querySelector(".voice-cmd-desc").value.trim();
    const url = row.querySelector(".voice-cmd-url").value.trim();
    if (name && url) {
      commands.push({ name, description, url });
    }
  });

  return {
    enabled: document.getElementById("voice-enabled").checked,
    provider: provider ? provider.value : "claude",
    claude_api_key: document.getElementById("claude-api-key").value,
    claude_model: document.getElementById("claude-model").value,
    openai_api_key: document.getElementById("openai-api-key").value,
    openai_model: document.getElementById("openai-model").value,
    whisper_model: document.getElementById("whisper-model").value,
    language: document.getElementById("voice-language").value,
    audio_device: document.getElementById("audio-device").value,
    commands,
  };
}

async function saveSettings() {
  const data = readVoiceConfig();
  await fetch("/api/voice-config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  closeSettings();
}

// Settings tabs
function switchSettingsTab(tab) {
  document.querySelectorAll("[data-stab]").forEach((t) => {
    t.classList.toggle("modal__tab--active", t.dataset.stab === tab);
  });
  document.getElementById("panel-commands").classList.toggle("settings-panel--hidden", tab !== "commands");
  document.getElementById("panel-config").classList.toggle("settings-panel--hidden", tab !== "config");
}

document.querySelectorAll("[data-stab]").forEach((t) => {
  t.addEventListener("click", () => switchSettingsTab(t.dataset.stab));
});

// Settings event listeners
document.getElementById("btn-settings").addEventListener("click", (e) => {
  e.stopPropagation();
  openSettings();
});
document.getElementById("settings-close").addEventListener("click", closeSettings);
document.getElementById("settings-cancel").addEventListener("click", closeSettings);
document.getElementById("settings-save").addEventListener("click", saveSettings);
document.getElementById("add-voice-command").addEventListener("click", () => {
  createVoiceCommandRow();
});
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) closeSettings();
});

/* ── Init ── */
loadConfig();
