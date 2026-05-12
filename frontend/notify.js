// L41 / R16：浏览器桌面通知。与飞书 channel 互补，二者独立可选。
//
// 主要职责：
//   1. 顶部条带：未授权 + state.config.push_channels.browser=true → 显示"启用"按钮
//   2. requestPermission：用户点击触发；结果持久化到 localStorage 防止反复弹
//   3. onPushEvent(env)：收到 backend 广播的 push_event 时弹 Notification toast
//
// 不弹的情况：
//   - push_channels.browser=false
//   - Notification.permission !== "granted"
//
// L41-HotFix：不再做 visible+focused 抑制。实际诉求是"和飞书行为对齐"——
// 飞书不会因为你 dashboard 开着就不推手机；浏览器也应一样无条件推。
// macOS 横幅 2-3 秒自动消失不打断输入，重复打扰可接受。
//
// L42 / R17 改进：
//   - tag = sid|ev_type|ts，避免同 session 多事件被覆盖成一条（飞书堆 3 张卡片，浏览器只剩 1 条 的根因）
//   - 维护最近 200 条 (sid,ev_type,ts) 的 dedupe Set，WS 重连补发时不重复弹
//   - onPushEvent 入口写 console.info，方便用户复现时定位是 backend 没广播还是前端没接住

const LS_KEY = "claude-notify.permBarDismissed";
let _dismissed = (typeof localStorage !== "undefined")
  && localStorage.getItem(LS_KEY) === "1";

// L42 / R17：(sid,ev_type,ts) dedupe，防 WS 重连补发时重复弹同一条通知
const _seenKeys = new Set();
const _seenOrder = [];
const _SEEN_CAP = 200;
function _markSeen(key) {
  if (_seenKeys.has(key)) return true;
  _seenKeys.add(key);
  _seenOrder.push(key);
  if (_seenOrder.length > _SEEN_CAP) {
    const drop = _seenOrder.shift();
    if (drop) _seenKeys.delete(drop);
  }
  return false;
}

function _supported() {
  return typeof window !== "undefined" && "Notification" in window;
}

function _channelOn(cfg) {
  const ch = (cfg && cfg.push_channels) || {};
  return ch.browser !== false; // 默认开
}

export function updatePermBar(cfg) {
  const bar = document.getElementById("notif-perm-bar");
  if (!bar) return;
  const should = _supported()
    && _channelOn(cfg)
    && Notification.permission === "default"
    && !_dismissed;
  bar.classList.toggle("hidden", !should);
}

export function bindPermBar(cfg) {
  const btn = document.getElementById("btn-notif-perm");
  const dismiss = document.getElementById("btn-notif-perm-dismiss");
  if (btn) {
    btn.addEventListener("click", async () => {
      if (!_supported()) return;
      try {
        await Notification.requestPermission();
      } catch (_) {}
      updatePermBar(cfg);
    });
  }
  if (dismiss) {
    dismiss.addEventListener("click", () => {
      _dismissed = true;
      try { localStorage.setItem(LS_KEY, "1"); } catch (_) {}
      updatePermBar(cfg);
    });
  }
}

const EVENT_LABEL = {
  Notification: "等待你的输入",
  Stop: "Claude 回合结束",
  SubagentStop: "子任务结束",
  TimeoutSuspect: "疑似 hang",
  SessionDead: "会话死亡",
  SessionEnd: "会话结束",
  TestNotify: "测试推送",
};

function _formatTitle(env) {
  const label = EVENT_LABEL[env.event_type] || env.event_type || "claude-notify";
  const who = env.title ? ` · ${env.title}` : "";
  return `${label}${who}`;
}

export function onPushEvent(env, getCfg) {
  // 诊断日志：哪一环丢了一眼看到（用户复现"飞书有浏览器没"时打开 console 即可）
  try {
    console.info("[notify] push_event recv", {
      sid: (env && env.session_id) ? env.session_id.slice(0, 8) : "",
      ev: env && env.event_type,
      reason: env && env.reason,
      ts: env && env.ts,
      perm: (typeof Notification !== "undefined") ? Notification.permission : "n/a",
    });
  } catch (_) {}

  if (!_supported()) { console.info("[notify] skip: Notification not supported"); return; }
  const cfg = (typeof getCfg === "function" ? getCfg() : getCfg) || {};
  if (!_channelOn(cfg)) { console.info("[notify] skip: channel off"); return; }
  if (Notification.permission !== "granted") {
    console.info("[notify] skip: permission =", Notification.permission); return;
  }

  // dedupe：补发场景下同一 (sid,ev_type,ts) 不重弹
  const key = [env.session_id || "", env.event_type || "", env.ts || ""].join("|");
  if (_markSeen(key)) { console.info("[notify] skip: dup", key); return; }

  // tag 加 ev_type+ts，让同 session 多事件能堆栈而不是互相覆盖
  const tag = key || "claude-notify";

  try {
    const n = new Notification(_formatTitle(env), {
      body: env.body || "",
      tag,
      silent: true,                              // 不响声音（用户明确要求）
    });
    n.onclick = () => {
      try { window.focus(); } catch (_) {}
      if (env.session_id) {
        try { window.location.hash = `s=${encodeURIComponent(env.session_id)}`; } catch (_) {}
      }
      n.close();
    };
  } catch (e) {
    console.warn("[notify] Notification ctor failed", e);
  }
}
