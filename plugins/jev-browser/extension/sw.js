// jev-browser extension: the daemon's hands in this Chrome. It connects to the daemon's local
// WebSocket (ws://127.0.0.1:<port>, with the pairing token), and relays CDP:
//   daemon -> {id, method, params, sessionId?}   ->  chrome.debugger.sendCommand on that tab
//   chrome.debugger events                        ->  {method, params, sessionId}
// Browser-level Target.* methods are not open to extensions, so the few the daemon uses are
// answered here with chrome.windows / chrome.tabs. Only tabs the daemon opened (and pop-ups
// they open) are ever attached, driven or closed.

let ws = null, connecting = false, backoff = 500;
const ours = new Set();                 // tab ids the daemon created or adopted as pop-ups
const sessions = new Map();             // sessionId -> tabId
const targetOfTab = new Map();          // tabId -> CDP target id
const sid = (tabId) => `ext-${tabId}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pairing() {
  const { pairing: p } = await chrome.storage.local.get("pairing");
  if (p?.port && p?.token) return p;
  // a test install ships its pairing beside the manifest (written by the daemon that loaded it)
  try {
    const r = await fetch(chrome.runtime.getURL("pair.json"));
    if (r.ok) { const j = await r.json(); if (j.port && j.token) { await chrome.storage.local.set({ pairing: j }); return j; } }
  } catch {}
  return null;
}

// "jevb:<port>:<token>" from `jevb pair`
export function parsePairing(s) {
  const m = String(s || "").trim().match(/^jevb:(\d{1,5}):([A-Za-z0-9_-]{20,})$/);
  return m ? { port: Number(m[1]), token: m[2] } : null;
}

const post = (msg) => { if (ws?.readyState === 1) ws.send(JSON.stringify(msg)); };

async function connect() {
  if (connecting || (ws && ws.readyState <= 1)) return;
  connecting = true;
  const p = await pairing();
  if (!p) { connecting = false; return; }
  const sock = new WebSocket(`ws://127.0.0.1:${p.port}/jevb?token=${encodeURIComponent(p.token)}`);
  sock.onopen = () => { ws = sock; connecting = false; backoff = 500; post({ method: "Extension.hello", params: { version: chrome.runtime.getManifest().version, userAgent: navigator.userAgent } }); };
  sock.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id == null) return;
    handle(m).then((result) => post({ id: m.id, result: result ?? {} }), (e) => post({ id: m.id, error: { message: String(e?.message ?? e) } }));
  };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    connecting = false;
    // the daemon went away: let go of every tab (the yellow bar goes with it)
    for (const tabId of sessions.values()) chrome.debugger.detach({ tabId }).catch(() => {});
    sessions.clear();
    setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 10_000);
  };
}

async function targetId(tabId) {
  if (targetOfTab.has(tabId)) return targetOfTab.get(tabId);
  for (let i = 0; i < 40; i++) {
    const t = (await chrome.debugger.getTargets()).find((x) => x.tabId === tabId);
    if (t) { targetOfTab.set(tabId, t.id); return t.id; }
    await sleep(25);
  }
  throw new Error(`no debugger target for tab ${tabId}`);
}
function tabOf(target) {
  for (const [tab, t] of targetOfTab) if (t === target) return tab;
  throw new Error(`unknown target ${target}`);
}

async function handle(m) {
  const p = m.params || {};
  if (m.sessionId) {
    const tabId = sessions.get(m.sessionId);
    if (tabId == null) throw new Error(`session ${m.sessionId} is not attached`);
    return chrome.debugger.sendCommand({ tabId }, m.method, p);
  }
  switch (m.method) {
    case "Browser.getVersion": return { product: (navigator.userAgent.match(/Chrome\/[\d.]+/) || [""])[0], userAgent: navigator.userAgent, protocolVersion: "1.3", transport: "extension" };
    case "Target.setDiscoverTargets": return {};
    // a fresh session: an incognito window when the user allowed this extension there;
    // otherwise null, and the session opens a normal window (the daemon logs it)
    case "Target.createBrowserContext":
      return { browserContextId: (await chrome.extension.isAllowedIncognitoAccess()) ? "incognito" : null };
    case "Target.disposeBrowserContext": return {};
    case "Target.createTarget": {
      const w = await chrome.windows.create({ url: "about:blank", focused: false, incognito: p.browserContextId === "incognito", width: 1280, height: 900 });
      const tab = w.tabs?.[0] ?? (await chrome.tabs.query({ windowId: w.id }))[0];
      ours.add(tab.id);
      return { targetId: await targetId(tab.id) };
    }
    case "Target.attachToTarget": {
      const tabId = tabOf(p.targetId);
      if (!ours.has(tabId)) throw new Error("not a tab the daemon opened");
      if (![...sessions.values()].includes(tabId)) await chrome.debugger.attach({ tabId }, "1.3");
      sessions.set(sid(tabId), tabId);
      return { sessionId: sid(tabId) };
    }
    case "Target.closeTarget": {
      const tabId = tabOf(p.targetId);
      if (!ours.has(tabId)) throw new Error("not a tab the daemon opened");
      await chrome.tabs.remove(tabId).catch(() => {});
      return { success: true };
    }
    case "Browser.close": {
      await Promise.all([...ours].map((t) => chrome.tabs.remove(t).catch(() => {})));
      return {};
    }
    default: throw new Error(`${m.method} is not available to the extension`);
  }
}

chrome.debugger.onEvent.addListener((src, method, params) => {
  if (src.tabId == null || src.sessionId || !sessions.has(sid(src.tabId))) return;
  post({ method, params, sessionId: sid(src.tabId) });
});
chrome.debugger.onDetach.addListener((src, reason) => {
  if (src.tabId == null || !sessions.has(sid(src.tabId))) return;
  sessions.delete(sid(src.tabId));
  post({ method: "Target.detachedFromTarget", params: { sessionId: sid(src.tabId), reason } });
});
// a pop-up (target=_blank, window.open) from a tab we drive is ours too
chrome.webNavigation.onCreatedNavigationTarget.addListener(async (d) => {
  if (!ours.has(d.sourceTabId)) return;
  ours.add(d.tabId);
  try {
    const [targetIdNew, opener] = await Promise.all([targetId(d.tabId), targetId(d.sourceTabId)]);
    post({ method: "Target.targetCreated", params: { targetInfo: { targetId: targetIdNew, type: "page", openerId: opener, url: d.url || "" } } });
  } catch {}
});
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!ours.has(tabId)) return;
  ours.delete(tabId);
  const t = targetOfTab.get(tabId); targetOfTab.delete(tabId);
  if (t) post({ method: "Target.targetDestroyed", params: { targetId: t } });
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg?.type === "pair") {
      const p = parsePairing(msg.value);
      if (!p) return { ok: false, error: "not a pairing code (it looks like jevb:47652:...)" };
      await chrome.storage.local.set({ pairing: p });
      try { ws?.close(); } catch {}
      ws = null; backoff = 500; connect();
      return { ok: true };
    }
    if (msg?.type === "status") {
      connect();
      const p = await pairing();
      return { ok: true, paired: !!p, port: p?.port ?? null, connected: ws?.readyState === 1, tabs: sessions.size };
    }
    return { ok: false };
  })().then(reply);
  return true;
});

// wake up and reconnect: on install/start, and every 30 s while the daemon is away
chrome.alarms.create("jevb-connect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
connect();
