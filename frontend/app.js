// 业务主入口。分层：api.js → 网络；format.js → 展示工具；app.js → 状态 + 渲染。
import { api, connectWS } from "./api.js";
import { onPushEvent as notifyOnPush, bindPermBar as notifyBindBar, updatePermBar as notifyUpdateBar } from "./notify.js";
import {
  relTime, absTimeShanghai, fullTimeShanghai,
  statusBadgeClass, statusLabel, eventBadgeClass,
  colorDot, colorDotValue, displayName, truncate, formatAge, stripMd,
} from "./format.js";
import { initNotes, onNotesEvent as onNotesWS } from "./notes.js";

// ───────────── localStorage 偏好 ─────────────
// L24（Round 6·B）：viewMode + collapsedGroups 持久化在浏览器
const LS_VIEW_MODE = "cn.viewMode";          // "list" | "grouped"
const LS_COLLAPSED = "cn.collapsedGroups";   // JSON array of cwd_short

function loadViewMode() {
  try {
    const v = localStorage.getItem(LS_VIEW_MODE);
    return (v === "grouped" || v === "list") ? v : "list";
  } catch { return "list"; }
}
function saveViewMode(v) {
  try { localStorage.setItem(LS_VIEW_MODE, v); } catch { /* ignore */ }
}
function loadCollapsedGroups() {
  try {
    const raw = localStorage.getItem(LS_COLLAPSED);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch { return new Set(); }
}
function saveCollapsedGroups(set) {
  try {
    localStorage.setItem(LS_COLLAPSED, JSON.stringify(Array.from(set)));
  } catch { /* ignore */ }
}

// ───────────── 状态 ─────────────
const state = {
  sessions: [],
  config: null,
  drawerSessionId: null,
  drawerEvents: [],
  drawerHiddenTypes: new Set(["Heartbeat", "SessionStart"]),
  filter: {
    text: "",
    statuses: new Set(["running", "waiting", "suspect", "idle"]),
  },
  viewMode: loadViewMode(),                   // L24：列表 / 按项目
  collapsedGroups: loadCollapsedGroups(),     // L24：折叠的 cwd_short 集合
  setupHealth: null,                          // L27：仅在 0 sessions 时拉一次，作首次接入引导
};

// status 排序权重：值越小越靠前
const STATUS_WEIGHT = {
  waiting: 0,
  suspect: 1,
  running: 2,
  idle: 3,
  ended: 4,
  dead: 5,
};
function statusWeight(s) {
  const w = STATUS_WEIGHT[s];
  return w === undefined ? 99 : w;
}

// notify_policy 静默秒数默认值
const SILENCE_DEFAULTS = {
  Notification: 5,
  Stop: 12,
  SubagentStop: 6,
  TimeoutSuspect: 5,
  SessionDead: 5,
  SessionEnd: 5,
};
const NP_KEYS = ["Notification","Stop","SubagentStop","TimeoutSuspect","SessionDead","SessionEnd"];

// ───────────── DOM ─────────────
const $ = (id) => document.getElementById(id);
const $sessions       = $("sessions");
const $webhookBadge   = $("webhook-badge");
const $wsBadge        = $("ws-badge");
const $btnTest        = $("btn-test");
const $btnConfig      = $("btn-config");
const $search         = $("search");
const $statusFilter   = $("status-filter");
const $summaryPill    = $("summary-pill");
const $summaryText    = $("summary-text");
const $btnMore        = $("btn-more");
const $moreMenu       = $("more-menu");

const $drawer         = $("drawer");
const $drawerDot      = $("drawer-dot");
const $drawerTitle    = $("drawer-title");
const $drawerStatus   = $("drawer-status");
const $drawerSub      = $("drawer-sub");
const $drawerMeta     = $("drawer-meta");
const $drawerMetaToggle = $("drawer-meta-toggle");
const $drawerMetaBody = $("drawer-meta-body");
const $drawerBody     = $("drawer-body");
const $drawerClose    = $("drawer-close");
const $drawerFocus    = $("drawer-focus");

const $modal          = $("modal-config");
const $configForm     = $("config-form");
const $configCancel   = $("config-cancel");
const $configMsg      = $("config-msg");
const $npFieldset     = $("np-fieldset");

const $modalAlias     = $("modal-alias");
const $aliasForm      = $("alias-form");
const $aliasCancel    = $("alias-cancel");

const $btnSnooze      = $("btn-snooze");
const $snoozeMenu     = $("snooze-menu");

const $toast          = $("toast");
const $drawerFilter   = $("drawer-filter");

let aliasEditingSid   = null;
let toastTimer        = null;

// ───────────── 渲染 ─────────────
function passesFilter(s) {
  if (!state.filter.statuses.has(s.status)) return false;
  const q = (state.filter.text || "").toLowerCase().trim();
  if (!q) return true;
  const hay = [
    s.alias, s.first_user_prompt, s.display_name, s.project,
    s.cwd, s.cwd_short, s.session_id, s.last_message,
  ].filter(Boolean).join("\n").toLowerCase();
  return hay.includes(q);
}

function renderSessions() {
  // 排序：一级 = STATUS_WEIGHT；二级 = 同 status 内按"等待时长"或"最近活动"
  // 二级语义随 status 切换：
  //   waiting / suspect / idle  → 按 last_event_ts 升序（等最久的往前，定位"等了 10 分钟没人理"）
  //   running / ended / dead    → 按 last_event_ts 降序（最近活动/最近退出的往前）
  const STALE_FIRST = new Set(["waiting", "suspect", "idle"]);
  const all = (state.sessions || []).slice().sort((a, b) => {
    const wa = statusWeight(a.status);
    const wb = statusWeight(b.status);
    if (wa !== wb) return wa - wb;
    const ta = new Date(a.last_event_ts || 0).getTime();
    const tb = new Date(b.last_event_ts || 0).getTime();
    // 同 status：waiting/suspect/idle 等最久的往前；其它按最新事件往前
    return STALE_FIRST.has(a.status) ? (ta - tb) : (tb - ta);
  });
  const visible = all.filter(passesFilter);
  if (visible.length === 0) {
    // 过滤后空 ≠ 真无 session：被过滤的还在 all 里
    const filteredAway = state.filter.text || state.filter.statuses.size < 6;
    if (filteredAway) {
      $sessions.innerHTML = `<p class="empty">${escapeHtml(`无匹配会话（${all.length} 个被过滤掉）`)}</p>`;
    } else if (all.length === 0) {
      // L27：真没 session → 渲染首次接入检查清单（health 还没拉就先拉一次）
      $sessions.innerHTML = renderSetupChecklist();
      bindSetupChecklistEvents();
      if (state.setupHealth === null) loadSetupHealth();
    } else {
      $sessions.innerHTML = `<p class="empty">${escapeHtml("暂无会话。等待第一个 hook 事件…")}</p>`;
    }
    updateDocTitle();
    renderSummaryPill();
    return;
  }
  // L24：grouped 视图按 cwd_short 分桶；list 视图保持平坦
  if (state.viewMode === "grouped") {
    $sessions.innerHTML = renderGroupedHTML(visible);
    bindGroupHeaderEvents();
  } else {
    $sessions.innerHTML = visible.map(sessionCardHTML).join("");
  }
  $sessions.querySelectorAll(".session-item").forEach(el => {
    el.addEventListener("click", (e) => {
      // 内部按钮自己处理
      if (e.target.closest(".alias-edit, .btn-focus, .btn-mute")) return;
      // 阻止冒泡到 document（document 监听 outside-click 关闭抽屉，会与本逻辑冲突）
      e.stopPropagation();
      openDrawer(el.dataset.sid);
    });
  });
  // L14：mute 按钮 → 弹小菜单
  $sessions.querySelectorAll(".btn-mute").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const sid = el.dataset.sid;
      if (sid) openMuteMenu(sid, el);
    });
  });
  $sessions.querySelectorAll(".alias-edit").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      onEditAlias(el.dataset.sid);
    });
  });
  $sessions.querySelectorAll(".btn-focus").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      if (el.disabled) return;
      onFocusTerminal(el.dataset.sid);
    });
  });
  // 卡片键盘可达：Enter / Space 打开抽屉
  $sessions.querySelectorAll(".session-item").forEach(el => {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDrawer(el.dataset.sid);
      }
    });
  });
  updateDocTitle();
  renderSummaryPill();
  // 若 URL hash 命中，应用高亮
  applyHashHighlight();
}

// ───────────── L24：按项目（cwd_short）分组视图 ─────────────
// 状态 → emoji 映射（与 user-guide 一致）
const STATUS_EMOJI = {
  waiting: "🟡",
  suspect: "🟠",
  running: "🟢",
  idle:    "⚪",
  ended:   "⚫",
  dead:    "🔴",
};
// 组的状态优先级 = 组内最紧急 status 的 weight（与 STATUS_WEIGHT 保持一致）
function groupMinWeight(sessions) {
  let m = 99;
  for (const s of sessions) {
    const w = statusWeight(s.status);
    if (w < m) m = w;
  }
  return m;
}
// 长 cwd 中间截断：保留头尾，中间打省略号
function truncateMid(s, max) {
  if (!s) return "";
  if (s.length <= max) return s;
  const keep = max - 1;          // 1 char for ellipsis
  const head = Math.ceil(keep * 0.45);
  const tail = keep - head;
  return s.slice(0, head) + "…" + s.slice(s.length - tail);
}

function renderGroupedHTML(visible) {
  // 按 cwd_short 分桶（空 cwd_short 归 "(未知 cwd)"）
  const buckets = new Map();
  for (const s of visible) {
    const key = (s.cwd_short || "").trim() || "(未知 cwd)";
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(s);
  }
  // 组排序：按组内最紧急 status weight；并列 → 按组内最新 last_event_ts 倒序
  const groups = Array.from(buckets.entries()).map(([cwd, sessions]) => ({
    cwd,
    sessions,
    weight: groupMinWeight(sessions),
    latestTs: sessions.reduce((acc, s) => {
      const t = new Date(s.last_event_ts || 0).getTime();
      return t > acc ? t : acc;
    }, 0),
  }));
  groups.sort((a, b) => {
    if (a.weight !== b.weight) return a.weight - b.weight;
    return b.latestTs - a.latestTs;
  });
  return groups.map(renderGroupHTML).join("");
}

function renderGroupHTML(g) {
  const collapsed = state.collapsedGroups.has(g.cwd);
  const cwdShown = truncateMid(g.cwd, 60);
  // 状态计数
  const counts = { waiting: 0, suspect: 0, running: 0, idle: 0, ended: 0, dead: 0 };
  for (const s of g.sessions) {
    if (counts[s.status] !== undefined) counts[s.status] += 1;
  }
  const order = ["waiting", "suspect", "running", "idle", "ended", "dead"];
  const summaryChips = order
    .filter(k => counts[k] > 0)
    .map(k => `<span class="summary-chip s-${k}" title="${escapeHtml(statusLabel(k))} ${counts[k]}">${STATUS_EMOJI[k]} ${counts[k]}</span>`)
    .join("");
  const total = g.sessions.length;
  const cwdAttr = escapeHtml(g.cwd);
  const caret = collapsed ? "▶" : "▼";
  const cls = `session-group${collapsed ? " is-collapsed" : ""}`;

  // 组内卡片：调用现有 sessionCardHTML（不改卡片本身）
  const cards = g.sessions.map(sessionCardHTML).join("");

  return `
    <section class="${cls}" data-cwd="${cwdAttr}">
      <button class="session-group-header" type="button" data-cwd="${cwdAttr}" aria-expanded="${collapsed ? "false" : "true"}" title="${cwdAttr}">
        <span class="group-caret" aria-hidden="true">${caret}</span>
        <span class="group-cwd">${escapeHtml(cwdShown)}</span>
        <span class="group-count">${total}</span>
        <span class="session-group-summary">${summaryChips}</span>
      </button>
      <div class="session-group-body">${cards}</div>
    </section>
  `;
}

function bindGroupHeaderEvents() {
  $sessions.querySelectorAll(".session-group-header").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const cwd = el.dataset.cwd;
      if (cwd != null) toggleGroup(cwd);
    });
  });
}

function toggleGroup(cwd) {
  if (state.collapsedGroups.has(cwd)) state.collapsedGroups.delete(cwd);
  else state.collapsedGroups.add(cwd);
  saveCollapsedGroups(state.collapsedGroups);
  renderSessions();
}

function setViewMode(mode) {
  if (mode !== "list" && mode !== "grouped") return;
  if (state.viewMode === mode) return;
  state.viewMode = mode;
  saveViewMode(mode);
  // 更新 toggle UI
  document.querySelectorAll(".view-toggle-btn").forEach(b => {
    const active = b.dataset.mode === mode;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  renderSessions();
}

function updateDocTitle() {
  const n = (state.sessions || []).filter(s =>
    s.status === "waiting" || s.status === "suspect"
  ).length;
  document.title = n > 0 ? `(${n}) claude-notify` : "claude-notify";
}

// L26（Round 7·A）：紧急度徽章。多 session 场景下让"等了多久"在视觉上立刻可读。
// 阈值（age in minutes）：
//   < 2min        → 不显示（避免噪音；正常回合刚结束的"喘息时间"）
//   2  ~ 5min     → hint    （浅黄）
//   5  ~ 15min    → warn    （橙色）
//   ≥ 15min       → critical（红色）
// status=waiting/suspect 在同一时长档上"再加深一档"——因为它们比 idle 更紧迫
// （waiting 已经卡在权限请求 / suspect 已经超过 timeout）。
// 仅 idle/waiting/suspect 三种 status 显示；其它（running/ended/dead）无意义。
function urgencyBadge(status, ageSeconds) {
  if (!["idle", "waiting", "suspect"].includes(status)) return "";
  const sec = Math.max(0, Math.floor(Number(ageSeconds) || 0));
  if (sec < 120) return "";  // < 2min 不显示

  // 基础档位
  let level;
  if (sec < 300)        level = 0; // 2-5min: hint
  else if (sec < 900)   level = 1; // 5-15min: warn
  else                  level = 2; // ≥ 15min: critical

  // waiting / suspect 升一档（饱和到 critical 不再升）
  if (status === "waiting" || status === "suspect") {
    level = Math.min(2, level + 1);
  }

  const cls = level === 0 ? "urgency-hint"
            : level === 1 ? "urgency-warn"
            : "urgency-critical";
  const text = formatAge(sec);
  const titleStatus = status === "waiting" ? "等输入"
                    : status === "suspect" ? "疑挂起"
                    : "回合结束";
  const title = `${titleStatus} 已 ${text}（${sec} 秒前最近事件）`;
  return `<span class="urgency-badge ${cls}" title="${escapeHtml(title)}">⏱ ${escapeHtml(text)}</span>`;
}

function sessionCardHTML(s) {
  // List view（v4 rework）：每行一个 session
  //   行 1：[●] [名字] [✎ hover 出现] [状态徽] ………………… [→ 终端]
  //   行 2：摘要（line-clamp 2，纯文字 label + · 分隔，去 chip 化）
  //   行 3：[cwd · mode · effort]  ……  [time · 心跳]
  const dotHtml = colorDot(s.color_token || 0);
  const status = s.status || "unknown";
  const statusCls = ` s-${escapeHtml(status)}`;
  const sid = s.session_id || "";
  const name = escapeHtml(displayName(s));
  const aliasInUse = !!(s.alias && s.alias.trim());

  // 副标题 = v3 spec §2.2 状态触发模板。主信息从工具层 → 语义层（last_milestone）。
  //   waiting   → 请你 → {next_action}
  //   running   → {last_milestone ? '上次 ${m}' : '正在 ${last_action}'} · 当前 {last_action || '等结果'}
  //   suspect   → 卡 {N} 分钟 · 上次 {last_action || '...'}
  //   idle/done → 刚完成 · {last_milestone || '...'} · 下一步 {next_action}
  //   idle/已歇 → 上次 · {last_milestone || '...'} · 等你 resume
  //   ended/dead→ 已结束 · {last_milestone || '可归档'}
  //   空        → 刚启动 · 等你输入第一条 prompt
  const ps = s.progress_state || "";
  // L39：所有摘要字段渲染前 strip markdown 控制符（**bold** / `code` / [text](url) 等）。
  // 原因：backend 直接抄 transcript 原文，会把裸的 ** 或 ` 暴露到卡片上。
  const waitingFor = stripMd(s.waiting_for).trim();
  const lastAction = stripMd(s.last_action).trim();
  const lastMilestone = stripMd(s.last_milestone).trim();
  const turnSummary = stripMd(s.turn_summary).trim();
  const nextAction = stripMd(s.next_action).trim();
  const lastMsg = stripMd(s.last_message).trim();
  const ageMin = Math.max(1, Math.floor((s.age_seconds || 0) / 60));

  let summary = "";
  let summaryLabel = "";
  let summaryRaw = "";

  if (s.status === "waiting") {
    summaryLabel = "请你 →";
    summary = nextAction || waitingFor || "确认";
    summaryRaw = waitingFor || nextAction;
  } else if (s.status === "running") {
    if (lastMilestone) {
      summaryLabel = "上次";
      summary = `${lastMilestone}${lastAction ? ` · 当前 ${lastAction}` : ""}`;
    } else {
      summaryLabel = "正在";
      summary = lastAction ? `${lastAction} · 等结果` : "工作中";
    }
    summaryRaw = lastMilestone || lastAction;
  } else if (s.status === "suspect") {
    summaryLabel = "可能卡住";
    summary = `卡 ${ageMin} 分钟${lastAction ? ` · 上次 ${lastAction}` : ""}`;
    summaryRaw = lastAction || lastMsg;
  } else if (s.status === "idle") {
    const milestone = lastMilestone || turnSummary;
    if (ps === "刚完成") {
      summaryLabel = "刚完成";
      summary = milestone ? `${milestone} · 下一步 ${nextAction || "提下一轮"}` : (nextAction || "提下一轮 / 或归档");
    } else {
      summaryLabel = "上次";
      summary = milestone ? `${milestone} · 等你 resume` : "等你 resume";
    }
    summaryRaw = stripMd(s.last_assistant_message) || milestone;
  } else if (s.status === "ended" || s.status === "dead") {
    summaryLabel = "已结束";
    summary = lastMilestone || turnSummary || "可归档";
    summaryRaw = stripMd(s.last_assistant_message) || lastMilestone;
  } else if (s.note) {
    summaryLabel = "备注"; summary = stripMd(s.note); summaryRaw = summary;
  } else {
    summaryLabel = "刚启动"; summary = "等你输入第一条 prompt";
  }

  const summaryHtml = `<div class="item-summary" title="${escapeHtml(summaryRaw || summary)}"><span class="item-summary-label">${escapeHtml(summaryLabel)}</span><span class="item-summary-text">${escapeHtml(summary)}</span></div>`;

  const hasTty = !!(s.tty && String(s.tty).trim());
  const focusBtnHtml = `<button class="btn-focus" data-sid="${escapeHtml(sid)}" type="button" ${hasTty ? "" : "disabled"} title="${hasTty ? "在终端中打开（切到对应 tab）" : "该 session 还没记录到 tty（再触发一次 hook 即可）"}" aria-label="打开终端">→ 终端</button>`;

  // L14：per-session 静音按钮
  const muteBtnHtml = renderMuteButton(s);

  // L26（Round 7·A）：紧急度徽章（仅 idle/waiting/suspect 且 age ≥ 2min）
  const urgencyHtml = urgencyBadge(status, s.age_seconds);

  // R11：菜单 prompt 紧急徽（Claude 正在等用户做菜单选择）
  const menuBadgeHtml = s.menu_detected === true
    ? `<span class="urgency-badge urgency-menu" title="Claude 等你做选择">🔥 指令选择·待响应</span>`
    : "";

  const metaLeftBits = escapeHtml(s.cwd_short || "");
  const fullTitle = fullTimeShanghai(s.last_event_ts);
  const metaRightBits = `<span title="${escapeHtml(fullTitle)}">${escapeHtml(relTime(s.last_event_ts))}</span>`;

  // L14：muted session 整体淡灰
  const muted = !!(s.mute_state && s.mute_state.muted);
  const mutedCls = muted ? " muted" : "";
  const mutedBadge = muted
    ? `<span class="mute-corner-badge" title="此会话已静音">${s.mute_state.scope === "stop_only" ? "🔕 仅 Stop" : "🔕"}</span>`
    : "";

  return `
    <div class="session-item${statusCls}${mutedCls}" data-sid="${escapeHtml(sid)}" tabindex="0" role="button" aria-label="${escapeHtml(name)}">
      <div class="item-head-l">
        ${dotHtml}
        <span class="name" title="${escapeHtml(sid)}">${name}</span>
        <button class="alias-edit" data-sid="${escapeHtml(sid)}" type="button" title="起别名/备注" aria-label="起别名">✎</button>
        <span class="${statusBadgeClass(status)}">${escapeHtml(statusLabel(status))}</span>
        ${menuBadgeHtml}
        ${mutedBadge}
      </div>
      <div class="item-actions">${urgencyHtml}${muteBtnHtml}${focusBtnHtml}</div>
      ${summaryHtml}
      <div class="item-meta">
        <span class="meta-left" title="${escapeHtml(s.cwd || "")}">${metaLeftBits || "—"}</span>
        <span class="meta-right">${metaRightBits}</span>
      </div>
    </div>
  `;
}

// L14：mute 按钮（已静音 → 🔕 + 剩余分钟；未静音 → 灰色 🔔）
function renderMuteButton(s) {
  const sid = s.session_id || "";
  const ms = s.mute_state || {};
  const muted = !!ms.muted;
  if (muted) {
    let label = "🔕";
    if (ms.until == null) {
      label += " 永久";
    } else {
      // 用 until 实时算剩余（30s 重渲染会刷新）；不依赖后端固定 remaining_seconds
      const rem = Math.max(0, Math.round((ms.until * 1000 - Date.now()) / 60000));
      label += ` ${rem}m`;
    }
    const scopeLabel = ms.scope === "stop_only" ? "（仅 Stop）" : "";
    const titleParts = [`已静音${scopeLabel}`];
    if (ms.until != null) {
      const d = new Date(ms.until * 1000);
      if (!Number.isNaN(d.getTime())) {
        const pad = (n) => String(n).padStart(2, "0");
        titleParts.push(`至 ${pad(d.getHours())}:${pad(d.getMinutes())}`);
      }
    } else {
      titleParts.push("永久（直到手动解除）");
    }
    titleParts.push("点击修改 / 解除");
    return `<button class="btn-mute is-muted" data-sid="${escapeHtml(sid)}" type="button" title="${escapeHtml(titleParts.join(" · "))}" aria-label="静音设置">${escapeHtml(label)}</button>`;
  }
  return `<button class="btn-mute" data-sid="${escapeHtml(sid)}" type="button" title="静音此会话" aria-label="静音此会话">🔔</button>`;
}

function renderDrawerEvents() {
  // autoscroll：最新事件置顶时，用户在最顶部就保持顶部锁定（auto-stick to newest）
  const wasAtTop = $drawerBody.scrollTop <= 4;

  // 重新渲染过滤 chip
  renderDrawerFilter();

  if (!state.drawerEvents || state.drawerEvents.length === 0) {
    $drawerBody.innerHTML = `<p class="muted">暂无事件。</p>`;
    return;
  }
  const filtered = state.drawerEvents.filter(ev =>
    !state.drawerHiddenTypes.has(ev.event)
  );
  if (filtered.length === 0) {
    $drawerBody.innerHTML = `<p class="muted">所有事件类型已被过滤。</p>`;
    return;
  }
  // 倒序：最新事件置顶（用户预期的「最近的在最上」）
  const rows = filtered.slice().reverse().map(eventRowHTML).join("");
  $drawerBody.innerHTML = rows;

  if (wasAtTop) {
    $drawerBody.scrollTop = 0;
  }
}

function renderDrawerFilter() {
  const types = Array.from(new Set(
    (state.drawerEvents || []).map(ev => ev.event).filter(Boolean)
  )).sort();
  if (types.length === 0) {
    $drawerFilter.innerHTML = "";
    return;
  }
  $drawerFilter.innerHTML = types.map(t => {
    const checked = state.drawerHiddenTypes.has(t) ? "" : "checked";
    return `<label><input type="checkbox" data-evtype="${escapeHtml(t)}" ${checked}/> ${escapeHtml(t)}</label>`;
  }).join("");
}

function eventRowHTML(ev) {
  // v3 spec §2.3：单行（chip + 时间 + 主文本）。主文本优先 LLM 摘要 > 启发式 message > 默认。
  // 砍掉的：
  //   - 事件类型默认 message（如「任务一回合结束」与 chip 重复）
  //   - LLM summary 与 message 实质相同时不再双行
  const evType = ev.event || "?";
  const msg = (ev.message || "").trim();
  const llmSummary = (ev.llm_summary || "").trim();

  // 与 chip 重复的默认中文化标题不进主文本
  const DEFAULT_NOISE = new Set([
    "任务一回合结束", "子 agent 完成", "Claude is waiting for your input",
    "会话开始", "会话结束", "会话疑似已结束", "heartbeat",
  ]);
  const cleanMsg = (msg && !DEFAULT_NOISE.has(msg) && !msg.startsWith("heartbeat:")) ? msg : "";

  // LLM 摘要 与 cleanMsg 实质相同（lower trim 等价 / 一方是另一方前缀）→ 只渲染一份
  let mainText = llmSummary || cleanMsg;
  if (llmSummary && cleanMsg && llmSummary !== cleanMsg) {
    const a = llmSummary.toLowerCase();
    const b = cleanMsg.toLowerCase();
    if (!a.startsWith(b) && !b.startsWith(a)) {
      // 都保留时 LLM 主、原文 hover 标题 — 不渲染第二行
    }
  }

  const titleAttr = (cleanMsg && cleanMsg !== mainText) ? ` title="${escapeHtml(cleanMsg)}"` : "";
  return `
    <div class="event-row"${titleAttr}>
      <div class="ev-head">
        <span class="${eventBadgeClass(evType)}">${escapeHtml(evType)}</span>
        <span class="muted">${escapeHtml(relTime(ev.ts))}</span>
      </div>
      ${mainText ? `<div class="ev-msg">${escapeHtml(mainText)}</div>` : ""}
    </div>
  `;
}

function setMenuStatus(el, text, kind) {
  // kind: 'ok' | 'warn' | '' (idle)
  if (!el) return;
  const span = el.querySelector(".menu-status-text");
  if (span) span.textContent = text;
  el.classList.remove("ok", "warn");
  if (kind === "ok") el.classList.add("ok");
  else if (kind === "warn") el.classList.add("warn");
}

function renderWebhookBadge() {
  const cfg = state.config;
  if (!cfg) {
    setMenuStatus($webhookBadge, "webhook: ?", "");
    return;
  }
  if (cfg.feishu_webhook_set) {
    setMenuStatus($webhookBadge, "webhook: 已配置", "ok");
  } else {
    setMenuStatus($webhookBadge, "webhook: 未配置", "warn");
  }
}

function setWsStatus(s) {
  const map = {
    connecting: ["ws: 连接中", ""],
    open:       ["ws: 已连接", "ok"],
    closed:     ["ws: 已断开", "warn"],
    error:      ["ws: 错误",   "warn"],
  };
  const [text, kind] = map[s] || ["ws: ?", ""];
  setMenuStatus($wsBadge, text, kind);
}

// 状态汇总徽：仅计 waiting + suspect（dead/ended 不计，避免拉高待办数字）
function renderSummaryPill() {
  const all = state.sessions || [];
  const wn = all.filter(s => s.status === "waiting").length;
  const sn = all.filter(s => s.status === "suspect").length;
  const total = all.length;
  $summaryPill.classList.remove("is-empty", "has-waiting", "has-suspect");
  if (wn > 0) $summaryPill.classList.add("has-waiting");
  else if (sn > 0) $summaryPill.classList.add("has-suspect");
  else $summaryPill.classList.add("is-empty");

  if (wn === 0 && sn === 0) {
    $summaryText.textContent = `全部 ${total}`;
  } else {
    const parts = [];
    if (wn > 0) parts.push(`等确认 ${wn}`);
    if (sn > 0) parts.push(`疑挂 ${sn}`);
    $summaryText.textContent = parts.join(" · ");
  }
}

// ───────────── 抽屉 ─────────────
async function openDrawer(sessionId) {
  state.drawerSessionId = sessionId;
  const s = state.sessions.find(x => x.session_id === sessionId);

  // 头部：dot / 名字 / 状态徽 + → 终端按钮
  if (s) {
    $drawerDot.style.background = colorDotValue(s.color_token || 0);
    $drawerTitle.textContent = displayName(s);
    $drawerTitle.title = sessionId;
    const status = s.status || "idle";
    $drawerStatus.className = statusBadgeClass(status);
    $drawerStatus.textContent = statusLabel(status);
    $drawerStatus.classList.remove("hidden");
    // → 终端按钮：根据 tty 是否存在决定 disabled
    if ($drawerFocus) {
      const hasTty = !!(s.tty && String(s.tty).trim());
      $drawerFocus.disabled = !hasTty;
      $drawerFocus.dataset.sid = sessionId;
      $drawerFocus.title = hasTty
        ? "在终端中打开（切到对应 tab）"
        : "该 session 还没记录到 tty（再触发一次 hook 即可）";
    }
  } else {
    $drawerDot.style.background = "var(--c-idle-solid)";
    $drawerTitle.textContent = sessionId;
    $drawerStatus.className = "status-badge status-idle hidden";
    $drawerStatus.textContent = "";
    if ($drawerFocus) {
      $drawerFocus.disabled = true;
      $drawerFocus.dataset.sid = sessionId;
    }
  }

  // 第一行：session id + cwd_short；第二行：与卡片同一句式（请你/正在/刚完成/...）
  const subLine1 = `session: ${truncate(sessionId, 12)}${s && s.cwd_short ? "  ·  " + s.cwd_short : ""}`;
  let subLine2 = "";
  if (s) {
    const ps = s.progress_state || "";
    const wf = (s.waiting_for || "").trim();
    const la = (s.last_action || "").trim();
    const ts = (s.turn_summary || "").trim();
    const na = (s.next_action || "").trim();
    const ageMin = Math.max(1, Math.floor((s.age_seconds || 0) / 60));
    let label = "", text = "";
    if (s.status === "waiting") {
      label = "请你"; text = na || wf || "确认";
    } else if (s.status === "running") {
      label = "正在"; text = la ? `${la} · 等结果` : "工作中";
    } else if (s.status === "suspect") {
      label = "可能卡住"; text = `${ageMin} 分钟无新事件 · 进终端看看`;
    } else if (s.status === "idle") {
      if (ps === "刚完成") { label = "刚完成"; text = ts ? `${ts} · 提下一轮` : "提下一轮 / 或归档"; }
      else { label = "上次"; text = ts ? `${ts} · 等你 resume` : "等你 resume"; }
    } else if (s.status === "ended" || s.status === "dead") {
      label = "已结束"; text = ts || na || "可归档 / 或 resume";
    } else if (s.alias && s.first_user_prompt) {
      label = "任务"; text = truncate(s.first_user_prompt, 80);
    }
    if (label && text) {
      subLine2 = `<span class="drawer-sub-label">${escapeHtml(label)}</span>${escapeHtml(text)}`;
    }
  }
  $drawerSub.innerHTML = `<div class="drawer-sub-line">${escapeHtml(subLine1)}</div>${subLine2 ? `<div class="drawer-sub-line drawer-sub-headline">${subLine2}</div>` : ""}`;
  $drawerSub.title = sessionId + (s && s.cwd ? "  ·  " + s.cwd : "");

  // Meta 块默认折叠
  $drawerMeta.classList.remove("open");
  $drawerMetaToggle.setAttribute("aria-expanded", "false");
  $drawerMetaBody.innerHTML = s ? renderMetaBody(s) : "";

  $drawer.classList.remove("hidden");
  $drawerBody.innerHTML = `<p class="empty">加载中…</p>`;
  try {
    const events = await api.listEvents(sessionId, 50);
    state.drawerEvents = Array.isArray(events) ? events : [];
    renderDrawerEvents();
  } catch (e) {
    $drawerBody.innerHTML = `<p class="empty">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

function fmtAbsTime(ts) {
  if (!ts) return "—";
  const t = new Date(ts);
  if (Number.isNaN(t.getTime())) return ts;
  const pad = (n) => String(n).padStart(2, "0");
  return `${t.getFullYear()}-${pad(t.getMonth()+1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
}

function metaRowHTML(key, val, opts = {}) {
  if (val == null || val === "") return "";
  const safeVal = escapeHtml(String(val));
  const valCls = opts.plain ? "val plain" : "val";
  const copyBtn = opts.copy
    ? `<button class="copy-btn" type="button" data-copy="${safeVal}" title="复制">复制</button>`
    : "";
  return `<div class="drawer-meta-row"><span class="key">${escapeHtml(key)}</span><span class="${valCls}">${safeVal}</span>${copyBtn}</div>`;
}

function renderMetaBody(s) {
  const modeBits = [s.permission_mode, s.effort_level].filter(Boolean).join(" · ");
  const rows = [
    metaRowHTML("sid", s.session_id, { copy: true }),
    metaRowHTML("cwd", s.cwd, { copy: true }),
    metaRowHTML("pid", s.claude_pid),
    metaRowHTML("transcript", s.transcript_path),
    metaRowHTML("mode", modeBits, { plain: true }),
    metaRowHTML("first_prompt", s.first_user_prompt, { plain: true }),
    metaRowHTML("起始", fmtAbsTime(s.start_ts || s.first_event_ts), { plain: true }),
    metaRowHTML("心跳", fmtAbsTime(s.last_heartbeat_ts), { plain: true }),
  ].filter(Boolean).join("");
  return rows || `<div class="drawer-meta-row"><span class="key muted">无</span></div>`;
}

function closeDrawer() {
  state.drawerSessionId = null;
  state.drawerEvents = [];
  $drawer.classList.add("hidden");
}

// ───────────── alias 编辑 ─────────────
async function onFocusTerminal(sessionId) {
  if (!sessionId) return;
  try {
    const r = await api.focusTerminal(sessionId);
    if (r && r.ok) {
      const appLabel = r.app === "iterm2" ? "iTerm2" : (r.app === "terminal" ? "Terminal.app" : "终端");
      showToast(`已切到 ${appLabel}（${r.tty}）`, "ok");
    } else {
      const reasonMap = {
        no_tty: "该 session 还没记录 tty。让 Claude 跑一次工具调用产生新 hook 即可。",
        tty_not_in_known_terminal: "iTerm2 / Terminal.app 中找不到该 tty（可能终端已关闭，或 Claude 跑在 VSCode/Termius 等不支持的环境）",
        osascript_timeout: "osascript 响应超时",
        osascript_not_available: "未检测到 osascript（仅 macOS 支持）",
      };
      const msg = (r && (reasonMap[r.reason] || r.reason)) || "未知错误";
      showToast(`无法定位终端：${msg}`, "err");
    }
  } catch (e) {
    showToast(`打开终端失败：${e.message}`, "err");
  }
}

// L14：per-session 静音弹小菜单
let muteMenuEl = null;
let muteMenuSid = null;

function closeMuteMenu() {
  if (muteMenuEl && muteMenuEl.parentNode) {
    muteMenuEl.parentNode.removeChild(muteMenuEl);
  }
  muteMenuEl = null;
  muteMenuSid = null;
}

function openMuteMenu(sessionId, anchorEl) {
  // 重复点击同一按钮关菜单；点其它按钮关旧的开新的
  if (muteMenuEl && muteMenuSid === sessionId) {
    closeMuteMenu();
    return;
  }
  closeMuteMenu();
  muteMenuSid = sessionId;
  const s = state.sessions.find(x => x.session_id === sessionId);
  const isMuted = !!(s && s.mute_state && s.mute_state.muted);

  const menu = document.createElement("div");
  menu.className = "mute-menu";
  menu.setAttribute("role", "menu");
  // 标题
  const heading = document.createElement("div");
  heading.className = "mute-menu-title";
  heading.textContent = isMuted ? "改静音 / 解除" : "静音此会话";
  menu.appendChild(heading);

  const items = [
    { label: "5 分钟",  minutes: 5,    scope: "all" },
    { label: "30 分钟", minutes: 30,   scope: "all" },
    { label: "60 分钟", minutes: 60,   scope: "all" },
    { label: "永久",    minutes: null, scope: "all" },
    { label: "仅 Stop · 30 分钟", minutes: 30, scope: "stop_only" },
  ];
  items.forEach(it => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "mute-menu-item";
    b.textContent = it.label;
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeMuteMenu();
      await applyMute(sessionId, it.minutes, it.scope);
    });
    menu.appendChild(b);
  });
  if (isMuted) {
    const sep = document.createElement("div");
    sep.className = "mute-menu-sep";
    menu.appendChild(sep);
    const off = document.createElement("button");
    off.type = "button";
    off.className = "mute-menu-item mute-menu-off";
    off.textContent = "解除静音";
    off.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeMuteMenu();
      await applyUnmute(sessionId);
    });
    menu.appendChild(off);
  }

  // 定位：相对 anchorEl 下方
  const r = anchorEl.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.top = `${Math.round(r.bottom + 4)}px`;
  menu.style.left = `${Math.round(r.left)}px`;
  menu.style.zIndex = "1000";
  document.body.appendChild(menu);
  muteMenuEl = menu;
}

async function applyMute(sessionId, minutes, scope) {
  try {
    await api.muteSession(sessionId, { minutes, scope });
    const text = (minutes == null)
      ? "已永久静音"
      : `已静音 ${minutes} 分钟${scope === "stop_only" ? "（仅 Stop）" : ""}`;
    showToast(text, "ok");
    await loadSessions();
  } catch (e) {
    showToast(`静音失败：${e.message}`, "err");
  }
}

async function applyUnmute(sessionId) {
  try {
    await api.unmuteSession(sessionId);
    showToast("已解除静音", "ok");
    await loadSessions();
  } catch (e) {
    showToast(`解除失败：${e.message}`, "err");
  }
}

function onEditAlias(sessionId) {
  const s = state.sessions.find(x => x.session_id === sessionId);
  aliasEditingSid = sessionId;
  $aliasForm.elements["alias"].value = (s && s.alias) || "";
  $aliasForm.elements["note"].value = (s && s.note) || "";
  $modalAlias.classList.remove("hidden");
  // 焦点到 alias 输入
  setTimeout(() => $aliasForm.elements["alias"].focus(), 0);
}

function closeAliasModal() {
  aliasEditingSid = null;
  $modalAlias.classList.add("hidden");
}

// ───────────── 配置弹窗 ─────────────
function openConfigModal() {
  if (!state.config) {
    $configMsg.textContent = "配置加载中…";
    return;
  }
  fillConfigForm(state.config);
  $configMsg.textContent = "";
  $modal.classList.remove("hidden");
}
function closeConfigModal() { $modal.classList.add("hidden"); }

function ensureNpRows() {
  if ($npFieldset.querySelector(".np-row")) return;
  // 注入 legend + 6 行控件
  const legend = $npFieldset.querySelector("legend") || document.createElement("legend");
  legend.textContent = "推送策略 (静默 = 等 N 秒后再推)";
  if (!legend.parentNode) $npFieldset.appendChild(legend);
  const frag = document.createDocumentFragment();
  NP_KEYS.forEach(k => {
    const row = document.createElement("div");
    row.className = "np-row";
    row.innerHTML = `
      <span class="np-name">${escapeHtml(k)}</span>
      <select data-np-mode="${escapeHtml(k)}">
        <option value="immediate">立即</option>
        <option value="silence">静默</option>
        <option value="off">关闭</option>
      </select>
      <input type="number" min="1" step="1" data-np-sec="${escapeHtml(k)}"
             value="${SILENCE_DEFAULTS[k] || 5}" />
      <span class="np-unit">秒</span>
    `;
    frag.appendChild(row);
  });
  $npFieldset.appendChild(frag);
  // 绑定 select 切换 → 启用/禁用秒数
  $npFieldset.addEventListener("change", (e) => {
    const t = e.target;
    if (t && t.matches("select[data-np-mode]")) {
      const k = t.dataset.npMode;
      const sec = $npFieldset.querySelector(`input[data-np-sec="${k}"]`);
      if (sec) sec.disabled = (t.value !== "silence");
    }
  });
}

function parseNpString(raw) {
  // 输入: "immediate" | "off" | "silence:N" | ""（视作默认 immediate）
  const s = (raw || "").trim();
  if (!s || s === "immediate") return { mode: "immediate", sec: 0 };
  if (s === "off") return { mode: "off", sec: 0 };
  if (s.startsWith("silence:")) {
    const n = parseInt(s.slice(8), 10);
    return { mode: "silence", sec: Number.isFinite(n) && n > 0 ? n : 5 };
  }
  // fallback：当作 immediate
  return { mode: "immediate", sec: 0 };
}

function fillConfigForm(cfg) {
  const f = $configForm;
  // L41 / R16：渠道开关
  const ch = cfg.push_channels || {};
  if (f.elements["channel_feishu"]) f.elements["channel_feishu"].checked = ch.feishu !== false;
  if (f.elements["channel_browser"]) f.elements["channel_browser"].checked = ch.browser !== false;
  f.elements["feishu_webhook"].value = cfg.feishu_webhook || "";
  f.elements["timeout_minutes"].value = cfg.timeout_minutes ?? 5;
  f.elements["dead_threshold_minutes"].value = cfg.dead_threshold_minutes ?? 30;

  ensureNpRows();
  const np = cfg.notify_policy || {};
  NP_KEYS.forEach(k => {
    const parsed = parseNpString(np[k]);
    const sel = $npFieldset.querySelector(`select[data-np-mode="${k}"]`);
    const sec = $npFieldset.querySelector(`input[data-np-sec="${k}"]`);
    if (sel) sel.value = parsed.mode;
    if (sec) {
      sec.value = parsed.sec || (SILENCE_DEFAULTS[k] || 5);
      sec.disabled = (parsed.mode !== "silence");
    }
  });

  // L15：quiet_hours
  const qh = cfg.quiet_hours || {};
  if (f.elements["qh_enabled"]) f.elements["qh_enabled"].checked = !!qh.enabled;
  if (f.elements["qh_start"]) f.elements["qh_start"].value = qh.start || "23:00";
  if (f.elements["qh_end"]) f.elements["qh_end"].value = qh.end || "08:00";
  if (f.elements["qh_tz"]) f.elements["qh_tz"].value = qh.tz || "Asia/Shanghai";
  const passSet = new Set(Array.isArray(qh.passthrough_events) ? qh.passthrough_events : ["Notification", "TimeoutSuspect"]);
  if (f.elements["qh_pass_notification"]) f.elements["qh_pass_notification"].checked = passSet.has("Notification");
  if (f.elements["qh_pass_timeout"]) f.elements["qh_pass_timeout"].checked = passSet.has("TimeoutSuspect");
  if (f.elements["qh_pass_test"]) f.elements["qh_pass_test"].checked = passSet.has("TestNotify");
  if (f.elements["qh_weekdays_only"]) f.elements["qh_weekdays_only"].checked = !!qh.weekdays_only;

  const disp = cfg.display || {};
  f.elements["display_show_first_prompt"].checked = disp.show_first_prompt !== false;
  f.elements["display_show_last_assistant"].checked = disp.show_last_assistant !== false;

  // LLM 配置
  const llm = cfg.llm || {};
  const ant = llm.anthropic || {};
  const oai = llm.openai || {};
  const lcli = llm.local_cli || {};
  if (f.elements["llm_enabled"]) f.elements["llm_enabled"].checked = !!llm.enabled;
  if (f.elements["llm_provider"]) f.elements["llm_provider"].value = llm.provider || "local_cli";
  if (f.elements["llm_anthropic_api_key"]) f.elements["llm_anthropic_api_key"].value = ant.api_key || "";
  if (f.elements["llm_anthropic_model"]) f.elements["llm_anthropic_model"].value = ant.model || "claude-haiku-4-5";
  if (f.elements["llm_anthropic_base_url"]) f.elements["llm_anthropic_base_url"].value = ant.base_url || "";
  if (f.elements["llm_openai_api_key"]) f.elements["llm_openai_api_key"].value = oai.api_key || "";
  if (f.elements["llm_openai_model"]) f.elements["llm_openai_model"].value = oai.model || "gpt-4o-mini";
  if (f.elements["llm_openai_base_url"]) f.elements["llm_openai_base_url"].value = oai.base_url || "https://api.openai.com";
  if (f.elements["llm_local_cli_binary"]) f.elements["llm_local_cli_binary"].value = lcli.binary || "";
  if (f.elements["llm_local_cli_model"]) f.elements["llm_local_cli_model"].value = lcli.model || "claude-haiku-4-5";
  if (f.elements["llm_local_cli_mode"]) f.elements["llm_local_cli_mode"].value = lcli.mode || "auto";
  if (f.elements["llm_local_cli_timeout_s"]) f.elements["llm_local_cli_timeout_s"].value = lcli.timeout_s || 30;
  if (f.elements["llm_topic_enabled"]) f.elements["llm_topic_enabled"].checked = llm.topic_enabled !== false;
  if (f.elements["llm_event_summary_enabled"]) f.elements["llm_event_summary_enabled"].checked = llm.event_summary_enabled !== false;
  syncProviderBlocks();
}

// 仅展示当前选中 provider 的配置块，其它折叠隐藏
function syncProviderBlocks() {
  const f = $configForm;
  if (!f) return;
  const providerSelect = f.elements["llm_provider"];
  if (!providerSelect) return;
  const current = providerSelect.value || "anthropic";
  f.querySelectorAll(".provider-block[data-provider]").forEach(block => {
    if (block.dataset.provider === current) block.classList.remove("hidden");
    else block.classList.add("hidden");
  });
}

function readConfigForm() {
  const f = $configForm;
  const np = {};
  NP_KEYS.forEach(k => {
    const sel = $npFieldset.querySelector(`select[data-np-mode="${k}"]`);
    const sec = $npFieldset.querySelector(`input[data-np-sec="${k}"]`);
    if (!sel) return;
    const mode = sel.value;
    if (mode === "immediate") np[k] = "immediate";
    else if (mode === "off") np[k] = "off";
    else if (mode === "silence") {
      const n = parseInt(sec && sec.value, 10);
      np[k] = `silence:${Number.isFinite(n) && n > 0 ? n : (SILENCE_DEFAULTS[k] || 5)}`;
    }
  });
  // 保留现有 snooze_until（不在表单里编辑，由免打扰按钮维护）
  const snooze_until = (state.config && state.config.snooze_until) || "";

  // L15：quiet_hours
  const passthrough_events = [];
  if (f.elements["qh_pass_notification"] && f.elements["qh_pass_notification"].checked) passthrough_events.push("Notification");
  if (f.elements["qh_pass_timeout"] && f.elements["qh_pass_timeout"].checked) passthrough_events.push("TimeoutSuspect");
  if (f.elements["qh_pass_test"] && f.elements["qh_pass_test"].checked) passthrough_events.push("TestNotify");
  const quiet_hours = {
    enabled: f.elements["qh_enabled"] ? f.elements["qh_enabled"].checked : false,
    start: (f.elements["qh_start"] && f.elements["qh_start"].value) || "23:00",
    end: (f.elements["qh_end"] && f.elements["qh_end"].value) || "08:00",
    tz: (f.elements["qh_tz"] && f.elements["qh_tz"].value.trim()) || "Asia/Shanghai",
    passthrough_events,
    weekdays_only: f.elements["qh_weekdays_only"] ? f.elements["qh_weekdays_only"].checked : false,
  };

  return {
    push_channels: {
      feishu: f.elements["channel_feishu"] ? f.elements["channel_feishu"].checked : true,
      browser: f.elements["channel_browser"] ? f.elements["channel_browser"].checked : true,
    },
    feishu_webhook: f.elements["feishu_webhook"].value.trim(),
    timeout_minutes: parseInt(f.elements["timeout_minutes"].value, 10) || 5,
    dead_threshold_minutes: parseInt(f.elements["dead_threshold_minutes"].value, 10) || 30,
    notify_policy: np,
    quiet_hours,
    display: {
      show_first_prompt: f.elements["display_show_first_prompt"].checked,
      show_last_assistant: f.elements["display_show_last_assistant"].checked,
    },
    llm: {
      enabled: f.elements["llm_enabled"] ? f.elements["llm_enabled"].checked : false,
      provider: f.elements["llm_provider"] ? f.elements["llm_provider"].value : "local_cli",
      anthropic: {
        api_key: f.elements["llm_anthropic_api_key"] ? f.elements["llm_anthropic_api_key"].value.trim() : "",
        model: f.elements["llm_anthropic_model"] ? (f.elements["llm_anthropic_model"].value.trim() || "claude-haiku-4-5") : "claude-haiku-4-5",
        base_url: f.elements["llm_anthropic_base_url"] ? f.elements["llm_anthropic_base_url"].value.trim() : "",
      },
      openai: {
        api_key: f.elements["llm_openai_api_key"] ? f.elements["llm_openai_api_key"].value.trim() : "",
        model: f.elements["llm_openai_model"] ? (f.elements["llm_openai_model"].value.trim() || "gpt-4o-mini") : "gpt-4o-mini",
        base_url: f.elements["llm_openai_base_url"] ? (f.elements["llm_openai_base_url"].value.trim() || "https://api.openai.com") : "https://api.openai.com",
      },
      local_cli: {
        binary: f.elements["llm_local_cli_binary"] ? f.elements["llm_local_cli_binary"].value.trim() : "",
        model: f.elements["llm_local_cli_model"] ? (f.elements["llm_local_cli_model"].value.trim() || "claude-haiku-4-5") : "claude-haiku-4-5",
        mode: f.elements["llm_local_cli_mode"] ? f.elements["llm_local_cli_mode"].value : "auto",
        timeout_s: f.elements["llm_local_cli_timeout_s"] ? (parseInt(f.elements["llm_local_cli_timeout_s"].value, 10) || 30) : 30,
      },
      topic_enabled: f.elements["llm_topic_enabled"] ? f.elements["llm_topic_enabled"].checked : true,
      event_summary_enabled: f.elements["llm_event_summary_enabled"] ? f.elements["llm_event_summary_enabled"].checked : true,
    },
    snooze_until,
  };
}

// ───────────── L27 首次接入引导 ─────────────
// 仅在 0 sessions 时显示。包含 3 项检查：hook 注册 / webhook 配置 / 等待事件。
function renderSetupChecklist() {
  const h = state.setupHealth;
  if (h === null) {
    return `<p class="empty">检查接入状态中…</p>`;
  }
  const hookOk = !!h.hooks_registered;
  const webhookOk = !!h.webhook_configured;
  const items = [
    {
      ok: hookOk,
      title: "Hook 已注册到 Claude Code",
      help: hookOk
        ? `已检测到 ${(h.hooks_detected || []).length} / ${(h.hooks_expected || []).length} 个事件 hook`
        : "未检测到 hook。请在终端执行：<code>python3 scripts/install-hooks.py</code>",
      action: hookOk ? "" : "重新检查",
      kind: "recheck",
    },
    {
      ok: webhookOk,
      title: "飞书 webhook 已配置",
      help: webhookOk
        ? "已配置（推送将走该 webhook）"
        : "未配置。点右下方按钮打开配置面板，粘贴飞书机器人 webhook URL",
      action: webhookOk ? "" : "去配置",
      kind: "open-config",
    },
    {
      ok: false,
      title: "等待第一个 hook 事件",
      help: "切到任何 Claude Code 终端，发一句话或触发任意工具调用，dashboard 会自动出现该 session",
      action: "",
      kind: "",
    },
  ];
  const lis = items.map((it, idx) => `
    <li class="setup-item ${it.ok ? "ok" : "todo"}">
      <span class="setup-mark" aria-hidden="true">${it.ok ? "✓" : (idx === items.length - 1 ? "…" : "✗")}</span>
      <div class="setup-text">
        <div class="setup-title">${escapeHtml(it.title)}</div>
        <div class="setup-help">${it.help}</div>
      </div>
      ${it.action ? `<button class="setup-action" data-action="${escapeHtml(it.kind)}">${escapeHtml(it.action)}</button>` : ""}
    </li>
  `).join("");
  return `
    <div class="setup-card">
      <div class="setup-header">首次接入检查</div>
      <ul class="setup-list">${lis}</ul>
      <div class="setup-foot">完成上述步骤后，dashboard 会自动显示 session。详细说明见 <code>docs/user-guide.md</code>。</div>
    </div>
  `;
}

function bindSetupChecklistEvents() {
  $sessions.querySelectorAll(".setup-action").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const kind = btn.dataset.action;
      if (kind === "open-config") {
        openConfigModal();
      } else if (kind === "recheck") {
        btn.disabled = true;
        btn.textContent = "检查中…";
        await loadSetupHealth();
      }
    });
  });
}

async function loadSetupHealth() {
  try {
    state.setupHealth = await api.getSetupHealth();
  } catch (e) {
    state.setupHealth = { hooks_registered: false, webhook_configured: false, session_count: 0 };
  }
  // 仅当当前还是空状态时重新渲染（避免把已加载的卡片覆盖）
  if ((state.sessions || []).length === 0) renderSessions();
}

// ───────────── 数据加载 ─────────────
async function loadSessions() {
  try {
    const list = await api.listSessions(true);
    state.sessions = Array.isArray(list) ? list : [];
    renderSessions();
  } catch (e) {
    $sessions.innerHTML = `<p class="empty">加载会话失败：${escapeHtml(e.message)}</p>`;
  }
}

async function loadConfig() {
  // P2-4：加载期间禁用配置按钮，避免用户极快点开看到"配置加载中…"
  $btnConfig.disabled = true;
  $btnConfig.title = "配置加载中…";
  try {
    state.config = await api.getConfig();
  } catch (e) {
    state.config = null;
  }
  $btnConfig.disabled = state.config == null;
  $btnConfig.title = state.config ? "" : "配置加载失败，刷新页面重试";
  renderWebhookBadge();
  renderSnoozeBadge();
}

// ───────────── toast ─────────────
function showToast(text, kind = "info") {
  $toast.textContent = text;
  $toast.className = "toast";
  if (kind === "ok") $toast.classList.add("toast-ok");
  else if (kind === "err") $toast.classList.add("toast-err");
  $toast.classList.remove("hidden");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $toast.classList.add("hidden");
    toastTimer = null;
  }, 3000);
}

// ───────────── 免打扰 ─────────────
function renderSnoozeBadge() {
  // spec §6：按钮文字直接显示状态。未静音 = "免打扰 ▾"；静音中 = "🔕 静音至 HH:MM"
  const until = state.config && state.config.snooze_until;
  let active = false;
  if (until) {
    const t = new Date(until).getTime();
    if (Number.isFinite(t) && t > Date.now()) {
      const d = new Date(t);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      $btnSnooze.textContent = `🔕 静音至 ${hh}:${mm}`;
      active = true;
    }
  }
  if (!active) $btnSnooze.textContent = "免打扰 ▾";
  $btnSnooze.classList.toggle("is-active", active);
}

function isoLocalPlus08(date) {
  // 输出 "+08:00" 偏移的 ISO 字符串（按本地时间字段，假定本地即东八区或保留本地壁钟）
  const pad = (n) => String(n).padStart(2, "0");
  const y = date.getFullYear();
  const mo = pad(date.getMonth() + 1);
  const d = pad(date.getDate());
  const h = pad(date.getHours());
  const mi = pad(date.getMinutes());
  const s = pad(date.getSeconds());
  return `${y}-${mo}-${d}T${h}:${mi}:${s}+08:00`;
}

function computeSnoozeUntil(kind) {
  if (kind === "off") return "";
  if (kind === "eod") {
    const now = new Date();
    const eod = new Date(now);
    eod.setHours(18, 0, 0, 0);
    if (eod.getTime() <= now.getTime()) {
      // 已过 18:00，则推到次日 18:00
      eod.setDate(eod.getDate() + 1);
    }
    return isoLocalPlus08(eod);
  }
  const minutes = parseInt(kind, 10);
  if (!Number.isFinite(minutes) || minutes <= 0) return "";
  const t = new Date(Date.now() + minutes * 60 * 1000);
  return isoLocalPlus08(t);
}

async function applySnooze(kind) {
  if (!state.config) {
    showToast("配置未加载", "err");
    return;
  }
  const snooze_until = computeSnoozeUntil(kind);
  // 复用现有 config，仅改 snooze_until
  const cfg = { ...state.config, snooze_until };
  // notify_policy/display 已在 state.config 里；不要带上 feishu_webhook 明文（保留服务端值）
  // 但 saveConfig 接口约定接受全字段；保留 feishu_webhook 为现有值（可能是空字符串占位）
  try {
    const saved = await api.saveConfig(cfg);
    state.config = saved || cfg;
    renderSnoozeBadge();
    showToast(snooze_until ? "已静音" : "已取消静音", "ok");
  } catch (e) {
    showToast(`设置失败：${e.message}`, "err");
  }
}

// ───────────── URL hash 高亮 ─────────────
function applyHashHighlight() {
  const m = (window.location.hash || "").match(/s=([^&]+)/);
  if (!m) return;
  const sid = decodeURIComponent(m[1]);
  const el = $sessions.querySelector(`.session-item[data-sid="${cssEscape(sid)}"]`);
  if (!el) return;
  el.classList.add("highlighted");
  try {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
  } catch (e) { /* ignore */ }
  setTimeout(() => el.classList.remove("highlighted"), 3000);
}

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
}

function onHashChange() {
  const m = (window.location.hash || "").match(/s=([^&]+)/);
  if (!m) return;
  const sid = decodeURIComponent(m[1]);
  openDrawer(sid);
  applyHashHighlight();
}

// ───────────── WS ─────────────
function onWsEnvelope(env) {
  if (!env || typeof env !== "object") return;
  if (env.type === "notes_updated") { onNotesWS(env); return; }
  // L41 / R16：后端决定推送 → 弹浏览器桌面通知（飞书与否独立）
  if (env.type === "push_event") {
    notifyOnPush(env, () => state.config || {});
    return;
  }
  if (env.type === "event") {
    const evt = env.event;
    const summary = env.session;
    if (!evt || !evt.session_id) return;
    upsertSessionFromEvent(evt, summary);
    renderSessions();
    if (state.drawerSessionId && evt.session_id === state.drawerSessionId) {
      state.drawerEvents.push(evt);
      if (state.drawerEvents.length > 50) state.drawerEvents = state.drawerEvents.slice(-50);
      renderDrawerEvents();
    }
    return;
  }
  if (env.type === "session_updated" && env.session) {
    const idx = state.sessions.findIndex(s => s.session_id === env.session.session_id);
    if (idx >= 0) state.sessions[idx] = env.session;
    else state.sessions.push(env.session);
    renderSessions();
    return;
  }
  if (env.type === "enrichment_updated") {
    // LLM 摘要补到位（topic 或 event）；更新对应 session + 抽屉事件流
    if (env.session && env.session.session_id) {
      const idx = state.sessions.findIndex(s => s.session_id === env.session.session_id);
      if (idx >= 0) state.sessions[idx] = env.session;
      renderSessions();
    }
    // 抽屉打开 + 同 session → 重新拉一次事件流以便附上 llm_summary
    if (state.drawerSessionId && env.session_id === state.drawerSessionId) {
      api.listEvents(state.drawerSessionId, 50).then(events => {
        state.drawerEvents = Array.isArray(events) ? events : [];
        renderDrawerEvents();
      }).catch(() => {});
    }
    return;
  }
  // hello / ping 不处理
}

function upsertSessionFromEvent(evt, summary) {
  if (summary && summary.session_id) {
    const idx = state.sessions.findIndex(s => s.session_id === summary.session_id);
    if (idx >= 0) state.sessions[idx] = summary;
    else state.sessions.push(summary);
    return;
  }
  const idx = state.sessions.findIndex(s => s.session_id === evt.session_id);
  if (idx >= 0) {
    const s = state.sessions[idx];
    s.last_event = evt.event;
    s.last_event_ts = evt.ts;
    s.event_count = (s.event_count || 0) + 1;
  } else {
    state.sessions.push({
      session_id: evt.session_id,
      project: evt.project || "",
      cwd: evt.cwd || "",
      cwd_short: evt.cwd_short || "",
      status: "running",
      last_event: evt.event,
      last_event_ts: evt.ts,
      event_count: 1,
    });
    loadSessions();
  }
}

// ───────────── 工具 ─────────────
function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// ───────────── 事件绑定 ─────────────
$btnTest.addEventListener("click", async () => {
  $btnTest.disabled = true;
  const orig = $btnTest.textContent;
  $btnTest.textContent = "推送中…";
  try {
    const r = await api.testNotify();
    if (r && r.ok) showToast("测试推送成功", "ok");
    else showToast(`测试推送返回：${JSON.stringify(r)}`, "err");
  } catch (e) {
    showToast(`测试推送失败：${e.message}`, "err");
  } finally {
    $btnTest.disabled = false;
    $btnTest.textContent = orig;
  }
});

$btnConfig.addEventListener("click", openConfigModal);
// LLM provider 切换 → 显示对应配置块
$configForm.addEventListener("change", (e) => {
  if (e.target && e.target.name === "llm_provider") syncProviderBlocks();
});
$configCancel.addEventListener("click", closeConfigModal);
$modal.addEventListener("click", (e) => { if (e.target === $modal) closeConfigModal(); });

$configForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $configMsg.textContent = "保存中…";
  try {
    const cfg = readConfigForm();
    const saved = await api.saveConfig(cfg);
    state.config = saved || cfg;
    state.setupHealth = null;  // L27：webhook 可能变了，让首次接入卡下次空状态出现时重新检测
    renderWebhookBadge();
    renderSnoozeBadge();
    notifyUpdateBar(state.config || {});
    $configMsg.textContent = "已保存。";
    showToast("配置已保存", "ok");
    setTimeout(closeConfigModal, 600);
  } catch (err) {
    $configMsg.textContent = `保存失败：${err.message}`;
    showToast(`保存失败：${err.message}`, "err");
  }
});

// alias modal 绑定
$aliasCancel.addEventListener("click", closeAliasModal);
$modalAlias.addEventListener("click", (e) => { if (e.target === $modalAlias) closeAliasModal(); });

$aliasForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!aliasEditingSid) { closeAliasModal(); return; }
  const sid = aliasEditingSid;
  const alias = $aliasForm.elements["alias"].value;
  const note = $aliasForm.elements["note"].value;
  try {
    await api.setAlias(sid, alias, note);
    closeAliasModal();
    await loadSessions();
    showToast("别名已保存", "ok");
  } catch (err) {
    showToast(`保存别名失败：${err.message}`, "err");
  }
});

// 免打扰按钮
$btnSnooze.addEventListener("click", (e) => {
  e.stopPropagation();
  closeMoreMenu();
  $snoozeMenu.classList.toggle("hidden");
  $btnSnooze.setAttribute("aria-expanded", String(!$snoozeMenu.classList.contains("hidden")));
});
$snoozeMenu.addEventListener("click", async (e) => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  const kind = t.getAttribute("data-snooze");
  if (!kind) return;
  $snoozeMenu.classList.add("hidden");
  $btnSnooze.setAttribute("aria-expanded", "false");
  await applySnooze(kind);
});

// L24：视图模式切换（list / grouped）
document.querySelectorAll(".view-toggle-btn").forEach(btn => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const mode = btn.dataset.mode;
    if (mode) setViewMode(mode);
  });
});

// 状态汇总徽：点击展开/折叠 chip 行
$summaryPill.addEventListener("click", (e) => {
  e.stopPropagation();
  const wasHidden = $statusFilter.classList.contains("hidden");
  $statusFilter.classList.toggle("hidden");
  $summaryPill.setAttribute("aria-expanded", String(wasHidden));
});

// ⋯ 菜单：点击展开/折叠
function closeMoreMenu() {
  $moreMenu.classList.add("hidden");
  $btnMore.setAttribute("aria-expanded", "false");
}
$btnMore.addEventListener("click", (e) => {
  e.stopPropagation();
  $snoozeMenu.classList.add("hidden");
  $btnSnooze.setAttribute("aria-expanded", "false");
  $moreMenu.classList.toggle("hidden");
  $btnMore.setAttribute("aria-expanded", String(!$moreMenu.classList.contains("hidden")));
});
// ⋯ 菜单内点 button 后关菜单（让原 click handler 仍然触发）
$moreMenu.addEventListener("click", (e) => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.tagName === "BUTTON") closeMoreMenu();
});

// outside click 统一关菜单 + 抽屉
document.addEventListener("click", (e) => {
  if (!$snoozeMenu.classList.contains("hidden")) {
    if (!$btnSnooze.contains(e.target) && !$snoozeMenu.contains(e.target)) {
      $snoozeMenu.classList.add("hidden");
      $btnSnooze.setAttribute("aria-expanded", "false");
    }
  }
  if (!$moreMenu.classList.contains("hidden")) {
    if (!$btnMore.contains(e.target) && !$moreMenu.contains(e.target)) {
      closeMoreMenu();
    }
  }
  // L14：mute 弹层 outside click 关闭
  if (muteMenuEl) {
    if (!muteMenuEl.contains(e.target) && !e.target.closest(".btn-mute")) {
      closeMuteMenu();
    }
  }
  // 抽屉打开时点空白关闭：排除抽屉本身、modal、卡片（卡片自己已 stopPropagation 切换 session）
  if (!$drawer.classList.contains("hidden")) {
    const t = e.target;
    if ($drawer.contains(t)) return;          // 抽屉内
    if (t.closest(".session-item")) return;   // 卡片由自身 click 切换
    if (t.closest(".modal")) return;          // 弹窗优先
    if (t.closest(".topbar")) return;         // 顶栏交互
    closeDrawer();
  }
});

// Meta 折叠
$drawerMetaToggle.addEventListener("click", () => {
  const open = $drawerMeta.classList.toggle("open");
  $drawerMetaToggle.setAttribute("aria-expanded", String(open));
});

// Meta 复制按钮（事件委托）
$drawerMetaBody.addEventListener("click", async (e) => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  if (!t.classList.contains("copy-btn")) return;
  const val = t.getAttribute("data-copy") || "";
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(val);
    } else {
      // fallback：临时 textarea
      const ta = document.createElement("textarea");
      ta.value = val; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
    }
    const orig = t.textContent;
    t.classList.add("copied");
    t.textContent = "已复制";
    setTimeout(() => {
      t.classList.remove("copied");
      t.textContent = orig || "复制";
    }, 1200);
  } catch (err) {
    showToast(`复制失败：${err.message}`, "err");
  }
});

// Esc 关闭抽屉 / 弹窗 / 菜单
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (muteMenuEl) { closeMuteMenu(); return; }
  if (!$snoozeMenu.classList.contains("hidden")) {
    $snoozeMenu.classList.add("hidden"); return;
  }
  if (!$moreMenu.classList.contains("hidden")) {
    closeMoreMenu(); return;
  }
  if (!$modalAlias.classList.contains("hidden")) {
    closeAliasModal(); return;
  }
  if (!$modal.classList.contains("hidden")) {
    closeConfigModal(); return;
  }
  if (!$drawer.classList.contains("hidden")) {
    closeDrawer(); return;
  }
});

$drawerClose.addEventListener("click", (e) => { e.stopPropagation(); closeDrawer(); });
if ($drawerFocus) {
  $drawerFocus.addEventListener("click", (e) => {
    e.stopPropagation();
    if ($drawerFocus.disabled) return;
    const sid = $drawerFocus.dataset.sid;
    if (sid) onFocusTerminal(sid);
  });
}

// 抽屉过滤 chip：勾掉 = 隐藏
$drawerFilter.addEventListener("change", (e) => {
  if (!(e.target instanceof HTMLInputElement)) return;
  const t = e.target.dataset.evtype;
  if (!t) return;
  if (e.target.checked) state.drawerHiddenTypes.delete(t);
  else state.drawerHiddenTypes.add(t);
  renderDrawerEvents();
});

$search.addEventListener("input", () => {
  state.filter.text = $search.value;
  renderSessions();
});

$statusFilter.addEventListener("change", (e) => {
  if (!(e.target instanceof HTMLInputElement)) return;
  if (e.target.checked) state.filter.statuses.add(e.target.value);
  else state.filter.statuses.delete(e.target.value);
  renderSessions();
});

// hashchange：飞书跳转过来时打开抽屉 + 高亮
window.addEventListener("hashchange", onHashChange);

// 30s 刷新相对时间显示
setInterval(renderSessions, 30 * 1000);
// 60s 兜底拉一次权威 sessions
setInterval(loadSessions, 60 * 1000);
// 60s 刷新静音徽章（过期自动隐藏）
setInterval(renderSnoozeBadge, 60 * 1000);

// 启动
(async function main() {
  setWsStatus("connecting");
  // L24：把 toggle UI 同步到 state.viewMode（从 localStorage 加载来的）
  document.querySelectorAll(".view-toggle-btn").forEach(b => {
    const active = b.dataset.mode === state.viewMode;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  await Promise.all([loadSessions(), loadConfig()]);
  // 处理 URL hash #s=<session_id>
  const m = (window.location.hash || "").match(/s=([^&]+)/);
  if (m) {
    openDrawer(decodeURIComponent(m[1]));
    applyHashHighlight();
  }
  // Round 10：记事本（独立模块，不阻塞 sessions 渲染）
  initNotes({ api, toast: showToast });
  // L41 / R16：浏览器桌面通知授权条带
  notifyBindBar(state.config || {});
  notifyUpdateBar(state.config || {});
  connectWS({ onEvent: onWsEnvelope, onStatus: setWsStatus });
})();
