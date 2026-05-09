// 纯展示工具：相对时间 / 状态徽章 / 色点 / 显示名。
// 视觉相关常量集中在这里。
// Phase 2：去 emoji 色点；statusBadgeClass 改返回 spec §4 token 化 class。

export function relTime(tsStr) {
  if (!tsStr) return "—";
  const t = new Date(tsStr).getTime();
  if (Number.isNaN(t)) return tsStr;
  let diff = Math.floor((Date.now() - t) / 1000);
  if (diff < 0) diff = 0;
  if (diff < 5) return "刚刚";
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

// 上海时间绝对时间（HH:MM 或 MM-DD HH:MM）
export function absTimeShanghai(tsStr) {
  if (!tsStr) return "";
  const t = new Date(tsStr);
  if (Number.isNaN(t.getTime())) return "";
  // 用 Intl 强制东八区，避免本机时区影响
  const fmt = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const parts = fmt.formatToParts(t).reduce((acc, p) => (acc[p.type] = p.value, acc), {});
  const today = new Date();
  const todayStr = `${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const dayStr = `${parts.month}-${parts.day}`;
  // 同一天只显示 HH:MM；跨天显示 MM-DD HH:MM
  return dayStr === todayStr ? `${parts.hour}:${parts.minute}` : `${dayStr} ${parts.hour}:${parts.minute}`;
}

// 上海时间完整：YYYY-MM-DD HH:MM:SS（用于 hover title）
export function fullTimeShanghai(tsStr) {
  if (!tsStr) return "";
  const t = new Date(tsStr);
  if (Number.isNaN(t.getTime())) return "";
  const fmt = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const parts = fmt.formatToParts(t).reduce((acc, p) => (acc[p.type] = p.value, acc), {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} +08:00`;
}

// 返回完整 className（含 status-badge + 状态色），按 spec §4 token 化
export function statusBadgeClass(status) {
  switch (status) {
    case "running": return "status-badge status-ok";
    case "waiting": return "status-badge status-waiting";
    case "idle":    return "status-badge status-idle";
    case "suspect": return "status-badge status-suspect";
    case "ended":   return "status-badge status-idle";
    case "dead":    return "status-badge status-dead";
    default:        return "status-badge status-idle";
  }
}

export function statusLabel(status) {
  const map = {
    running: "运行中",
    waiting: "等确认",
    idle: "待命",
    suspect: "疑挂起",
    ended: "已退出",
    dead: "已死亡",
  };
  return map[status] || status || "未知";
}

// 事件徽章（用在抽屉 ev 行）
export function eventBadgeClass(ev) {
  switch (ev) {
    case "Notification":    return "status-badge status-waiting";
    case "Stop":            return "status-badge status-ok";
    case "SubagentStop":    return "status-badge status-ok";
    case "TimeoutSuspect":  return "status-badge status-suspect";
    case "SessionDead":     return "status-badge status-dead";
    case "SessionEnd":      return "status-badge status-idle";
    case "SessionStart":    return "status-badge status-idle";
    case "Heartbeat":       return "status-badge status-idle";
    default:                return "status-badge status-idle";
  }
}

// sid hash 8 色降饱和（spec §5）—— 与后端 color_token 索引 0-7 对齐
// 飞书侧仍发 emoji（在 backend/feishu.py），同 token 索引保证同色
export const DOT_COLORS = [
  "#c45a4a", "#c47a3a", "#b89a3a", "#6a8f55",
  "#4a7fb0", "#8a6db0", "#8a6a4a", "#555550",
];

// 返回完整 <span class="dot" style="background:...">；注意调用方需要插入到 innerHTML
// （colorDot 既不再返回 emoji，也不需要 escapeHtml 包裹——颜色值 / class 都是受控字符串）
export function colorDot(token) {
  const i = ((token | 0) % DOT_COLORS.length + DOT_COLORS.length) % DOT_COLORS.length;
  return `<span class="dot" style="background:${DOT_COLORS[i]}"></span>`;
}

// 返回纯色值，给抽屉头部 dot inline 用
export function colorDotValue(token) {
  const i = ((token | 0) % DOT_COLORS.length + DOT_COLORS.length) % DOT_COLORS.length;
  return DOT_COLORS[i];
}

export function displayName(s) {
  if (!s) return "(unknown)";
  return s.display_name || s.alias || s.project || (s.session_id || "").slice(0, 8) || "(unknown)";
}

export function truncate(s, n) {
  if (s == null) return "";
  s = String(s);
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}
