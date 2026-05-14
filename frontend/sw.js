// R25 / L50：Service Worker — 让 OS Notification 点击能可靠把 dashboard 那个 Chrome window 切前台。
//
// 根因（替代 notify.js 的 new Notification(...) + window.focus()）：
//   - new Notification(...) 的 onclick 在创建通知的 tab 上下文里跑；
//   - Chrome 对"后台 window 脚本自我切前台"有限制：点通知时只把通知所在 window 拉前台，
//     dashboard 那个 window 在背景里时 window.focus() 多半切不动；
//   - 用户看到的是最近活跃的 Chrome window（恰好装着别的 localhost 服务）而非 dashboard。
//
// 标准做法：SW 注册的通知，点击事件跑在 SW 上下文，SW 调 client.focus() 是允许跨 window
// 切前台的（user activation 在 notificationclick 上下文可传递）。同时 clients.matchAll
// 是严格同源（dashboard origin）扫描，天生过滤掉别的 localhost 端口。

const SW_TAG = "[sw]";

self.addEventListener("install", (event) => {
  // 新版本立即接管，不等所有旧 tab 关掉
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // 立刻接管所有同源 window client
  event.waitUntil(self.clients.claim());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const sid = data.sid || "";
  const scope = self.registration.scope;  // e.g. "http://127.0.0.1:8787/"
  const targetUrl = sid
    ? `${scope}#s=${encodeURIComponent(sid)}`
    : scope;

  event.waitUntil((async () => {
    try {
      const all = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // matchAll 是同源（dashboard origin）扫描；这里再用 scope 前缀过滤一遍，
      // 把 dashboard 子路径之外的（理论上 origin 内不会有，但保险）都剔掉
      const candidates = all.filter(c => c.url && c.url.startsWith(scope));
      if (candidates.length > 0) {
        // 优先选 visibilityState === "visible" 的；否则取第一个
        const visible = candidates.find(c => c.visibilityState === "visible");
        const target = visible || candidates[0];
        try {
          // postMessage 先发，确保 dashboard tab 立刻刷数据 + 跳 hash；
          // 即便后面 focus() 在某些边缘环境失败，UI 状态也是对的
          target.postMessage({ type: "NOTIFICATION_CLICK", sid });
        } catch (e) {
          console.warn(SW_TAG, "postMessage failed", e);
        }
        try {
          await target.focus();
        } catch (e) {
          console.warn(SW_TAG, "client.focus failed", e);
        }
        return;
      }
      // 没现成 dashboard tab → 开新 tab
      await self.clients.openWindow(targetUrl);
    } catch (e) {
      console.error(SW_TAG, "notificationclick handler error", e);
    }
  })());
});
