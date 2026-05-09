// API 层：所有后端调用集中在这里。
const BASE = window.location.origin;

async function jsonFetch(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${path}: ${text}`);
  }
  return res.json();
}

export const api = {
  listSessions: (includeTerminal = true) =>
    jsonFetch(`/api/sessions?include_terminal=${includeTerminal ? 1 : 0}`),
  listEvents: (sessionId, limit = 50) =>
    jsonFetch(`/api/events?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`),
  listDecisions: (sessionId, limit = 50) =>
    jsonFetch(`/api/sessions/${encodeURIComponent(sessionId)}/decisions?limit=${limit}`),
  getConfig: () => jsonFetch("/api/config"),
  saveConfig: (cfg) =>
    jsonFetch("/api/config", { method: "POST", body: JSON.stringify(cfg) }),
  testNotify: () => jsonFetch("/api/test-notify", { method: "POST" }),
  setAlias: (sessionId, alias, note) =>
    jsonFetch(`/api/sessions/${encodeURIComponent(sessionId)}/alias`, {
      method: "POST",
      body: JSON.stringify({ alias, note }),
    }),
  focusTerminal: (sessionId) =>
    jsonFetch(`/api/sessions/${encodeURIComponent(sessionId)}/focus-terminal`, {
      method: "POST",
    }),
  // L14: per-session 静音
  // minutes: number (分钟) | null（永久）；scope: "all" | "stop_only"
  muteSession: (sessionId, { minutes = null, scope = "all", label = "" } = {}) =>
    jsonFetch(`/api/sessions/${encodeURIComponent(sessionId)}/mute`, {
      method: "POST",
      body: JSON.stringify({ minutes, scope, label }),
    }),
  unmuteSession: (sessionId) =>
    jsonFetch(`/api/sessions/${encodeURIComponent(sessionId)}/mute`, {
      method: "DELETE",
    }),
};

// WebSocket：自动 5s 重连，回调收到完整 envelope
export function connectWS({ onEvent, onStatus }) {
  let ws = null;
  let stopped = false;
  let retryTimer = null;

  function setStatus(s) { onStatus && onStatus(s); }

  function open() {
    if (stopped) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws`;
    setStatus("connecting");
    try {
      ws = new WebSocket(url);
    } catch (e) {
      setStatus("error");
      scheduleRetry();
      return;
    }
    ws.onopen = () => setStatus("open");
    ws.onmessage = (msg) => {
      try {
        const envelope = JSON.parse(msg.data);
        onEvent && onEvent(envelope);
      } catch (e) {
        // 忽略畸形消息
      }
    };
    ws.onclose = () => {
      setStatus("closed");
      scheduleRetry();
    };
    ws.onerror = () => {
      setStatus("error");
    };
  }

  function scheduleRetry() {
    if (stopped) return;
    if (retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, 5000);
  }

  open();
  return {
    close() {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) ws.close();
    },
  };
}
