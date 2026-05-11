// Round 10：记事本模块。
// 单 textarea + autosave debounce 800ms + WS 静默覆盖。
// SSOT 在后端；本模块只管：
//   - 内存最新 content
//   - 折叠态（localStorage 持久化）
//   - WS notes_updated → 视情况覆盖
//   - 保存状态 UI

const LS_COLLAPSED = "cn.notesCollapsed";
const DEBOUNCE_MS = 800;

const state = {
  content: "",
  saveTimer: null,
  saving: false,
  dirty: false,
  collapsed: false,
  textareaFocused: false,
  size: 0,
  updatedAt: "",
};

let deps = null;  // { api, toast }
let $panel, $toggle, $textarea, $saveState, $size;

function loadCollapsed() {
  try {
    return localStorage.getItem(LS_COLLAPSED) === "1";
  } catch { return false; }
}
function saveCollapsedLS(v) {
  try { localStorage.setItem(LS_COLLAPSED, v ? "1" : "0"); } catch { /* ignore */ }
}

function byteLen(s) {
  // 与后端一致：UTF-8 byte 计数
  try { return new TextEncoder().encode(s || "").length; }
  catch { return (s || "").length; }
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function setSaveStateUI(kind) {
  // kind: "idle" | "saving" | "saved" | "error"
  if (!$saveState) return;
  $saveState.classList.remove("is-saving", "is-saved", "is-error");
  if (kind === "saving") {
    $saveState.classList.add("is-saving");
    $saveState.textContent = "保存中…";
  } else if (kind === "saved") {
    $saveState.classList.add("is-saved");
    $saveState.textContent = "已保存";
  } else if (kind === "error") {
    $saveState.classList.add("is-error");
    $saveState.textContent = "未保存";
  } else {
    $saveState.textContent = "已保存";
  }
}

function renderSize() {
  if ($size) $size.textContent = formatSize(state.size);
}

function applyCollapsed() {
  if (!$panel || !$toggle) return;
  $panel.classList.toggle("is-collapsed", state.collapsed);
  $toggle.setAttribute("aria-expanded", state.collapsed ? "false" : "true");
}

function scheduleSave() {
  state.dirty = true;
  if (state.saveTimer) clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSave, DEBOUNCE_MS);
}

async function flushSave() {
  state.saveTimer = null;
  if (state.saving) {
    // 若上一笔仍在飞行，800ms 后再试；input 事件会继续刷 timer
    state.saveTimer = setTimeout(flushSave, DEBOUNCE_MS);
    return;
  }
  const content = state.content;
  state.saving = true;
  setSaveStateUI("saving");
  try {
    const r = await deps.api.putNotes(content);
    state.saving = false;
    state.dirty = false;
    state.size = (r && typeof r.size === "number") ? r.size : byteLen(content);
    state.updatedAt = (r && r.updated_at) || "";
    renderSize();
    setSaveStateUI("saved");
  } catch (e) {
    state.saving = false;
    setSaveStateUI("error");
    // 静默重试策略：不自动重试；用户继续编辑会再触发 debounce，自然重发。
    if (deps && deps.toast) {
      const msg = (e && e.message) || "保存失败";
      deps.toast(`记事本保存失败：${msg}`, "err");
    }
  }
}

async function fetchAndApply() {
  try {
    const r = await deps.api.getNotes();
    const content = (r && typeof r.content === "string") ? r.content : "";
    state.content = content;
    state.size = (r && typeof r.size === "number") ? r.size : byteLen(content);
    state.updatedAt = (r && r.updated_at) || "";
    if ($textarea) $textarea.value = content;
    renderSize();
    setSaveStateUI("saved");
  } catch (e) {
    setSaveStateUI("error");
    if (deps && deps.toast) {
      deps.toast(`记事本加载失败：${(e && e.message) || ""}`, "err");
    }
  }
}

export function initNotes({ api, toast }) {
  deps = { api, toast };
  $panel    = document.getElementById("notes-panel");
  $toggle   = document.getElementById("notes-toggle");
  $textarea = document.getElementById("notes-textarea");
  $saveState = document.getElementById("notes-save-state");
  $size     = document.getElementById("notes-size");
  if (!$panel || !$textarea) return;

  state.collapsed = loadCollapsed();
  applyCollapsed();

  $toggle && $toggle.addEventListener("click", () => {
    state.collapsed = !state.collapsed;
    saveCollapsedLS(state.collapsed);
    applyCollapsed();
  });

  $textarea.addEventListener("input", () => {
    state.content = $textarea.value;
    state.size = byteLen(state.content);
    renderSize();
    scheduleSave();
  });
  $textarea.addEventListener("focus", () => { state.textareaFocused = true; });
  $textarea.addEventListener("blur",  () => { state.textareaFocused = false; });

  fetchAndApply();
}

export function setCollapsed(collapsed) {
  state.collapsed = !!collapsed;
  saveCollapsedLS(state.collapsed);
  applyCollapsed();
}

export function onNotesEvent(envelope) {
  // WS notes_updated：只有"未聚焦 + 未脏"时才覆盖；否则静默忽略（不打扰用户输入）。
  if (!envelope || envelope.type !== "notes_updated") return;
  if (state.textareaFocused) return;
  if (state.dirty) return;
  if (state.saving) return;
  fetchAndApply();
}
