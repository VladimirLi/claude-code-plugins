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
const contexts = new Map();             // fresh-session context id -> its tab ids
let contextSeq = 0;
// A fresh session gets its own incognito window, the same in your Chrome and in the test Chrome.
// Chrome keeps one incognito profile while any incognito window is open, so the extension keeps
// one blank, minimized incognito window (the keeper) open while the daemon is connected. Each
// session's window then joins a live incognito profile: its HTTP cache stays warm from earlier
// sessions, and Chrome does not build a new profile per session. Cookies and storage do not
// carry over: when a session closes (and before one starts, if that was missed), the extension
// clears the incognito profile's cookies, and every kind of storage of each origin the sessions
// visited, through chrome.debugger on the keeper tab. Your normal profile is never touched.
let keeper = null;                      // {windowId, tabId, attached}
const visited = new Set();              // origins fresh sessions loaded since the last clear
let dirty = false;                      // fresh sessions ran since the incognito profile was last cleared
const sid = (tabId) => `ext-${tabId}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pairing() {
  const { pairing: p } = await chrome.storage.local.get("pairing");
  if (p?.port && p?.token) return p;
  // a test install ships its pairing beside the manifest (written by the daemon that loaded it)
  try {
    const r = await fetch(chrome.runtime.getURL("pair.json"));
    if (r.ok) {
      const j = await r.json();
      if (j.port && j.token) { await chrome.storage.local.set({ pairing: j }); return j; }
    }
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
    dropKeeper();
    setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 10_000);
  };
}

async function targetId(tabId) {
  if (targetOfTab.has(tabId)) return targetOfTab.get(tabId);
  // the target exists once the tab does; retry only in the rare case it is not listed yet
  for (let i = 0; i < 40; i++) {
    const t = (await chrome.debugger.getTargets()).find((x) => x.tabId === tabId);
    if (t) { targetOfTab.set(tabId, t.id); return t.id; }
    await sleep(10);
  }
  throw new Error(`no debugger target for tab ${tabId}`);
}
// The keeper: a blank incognito window that holds the incognito profile open between sessions.
async function ensureKeeper() {
  if (keeper) {
    try { await chrome.tabs.get(keeper.tabId); return keeper; } catch { keeper = null; }
  }
  const make = (o) => chrome.windows.create({ url: "about:blank", focused: false, incognito: true, ...o });
  const w = await make({ state: "minimized" }).catch(() => make({ width: 400, height: 300 }));
  if (!w.incognito) { await chrome.windows.remove(w.id).catch(() => {}); throw new Error("Chrome did not open an incognito window for the fresh session"); }
  const tab = w.tabs?.[0] ?? (await chrome.tabs.query({ windowId: w.id }))[0];
  return (keeper = { windowId: w.id, tabId: tab.id, attached: false });
}
function dropKeeper() {
  const k = keeper; keeper = null;
  if (k) chrome.windows.remove(k.windowId).catch(() => {});
}
// incognito tabs that are neither the keeper nor a tab the daemon drives: your own
async function foreignIncognito() {
  return (await chrome.tabs.query({})).filter((t) => t.incognito && t.id !== keeper?.tabId && !ours.has(t.id)).length;
}
const STORAGE = "all";
// Empty the incognito profile's cookies and the storage of every origin fresh sessions visited.
// Never while another fresh session is open, or while you have your own incognito tabs (it would
// log you out there): then the profile stays dirty, and the next fresh session says it shares it.
async function clearIncognito() {
  if (!dirty) return null;
  if (contexts.size) return "another fresh session is open: cookies and storage were not cleared";
  const mine = await foreignIncognito();
  if (mine) return `you have ${mine} incognito tab(s) of your own open: cookies and storage were not cleared`;
  const k = await ensureKeeper();
  const target = { tabId: k.tabId };
  if (!k.attached) { await chrome.debugger.attach(target, "1.3"); k.attached = true; }
  const origins = [...visited];
  await Promise.all([
    chrome.debugger.sendCommand(target, "Network.clearBrowserCookies"),
    ...origins.map((origin) => chrome.debugger.sendCommand(target, "Storage.clearDataForOrigin", { origin, storageTypes: STORAGE })),
  ]);
  for (const o of origins) visited.delete(o);
  dirty = false;
  return null;
}
function tabOf(target) {
  for (const [tab, t] of targetOfTab) if (t === target) return tab;
  throw new Error(`unknown target ${target}`);
}

export async function handle(m) {
  const p = m.params || {};
  if (m.sessionId) {
    const tabId = sessions.get(m.sessionId);
    if (tabId == null) throw new Error(`session ${m.sessionId} is not attached`);
    return chrome.debugger.sendCommand({ tabId }, m.method, p);
  }
  switch (m.method) {
    case "Browser.getVersion": return { product: (navigator.userAgent.match(/Chrome\/[\d.]+/) || [""])[0], userAgent: navigator.userAgent, protocolVersion: "1.3", transport: "extension" };
    case "Target.setDiscoverTargets": return {};
    // a fresh session: its own incognito window (see the keeper above). Without incognito
    // access there is no empty context to give, so refuse, never hand out your profile.
    case "Target.createBrowserContext": {
      if (!(await chrome.extension.isAllowedIncognitoAccess()))
        throw new Error("--fresh needs the jev-browser extension allowed in Incognito (chrome://extensions, jev-browser, Details, \"Allow in Incognito\"); without it a session can only use your normal profile");
      await ensureKeeper();
      const warning = await clearIncognito();
      const id = `incognito-${++contextSeq}`;
      contexts.set(id, new Set());
      return { browserContextId: id, isolate: "incognito", ...(warning ? { warning: `shares cookies and storage: ${warning}` } : {}) };
    }
    case "Target.disposeBrowserContext": {
      const tabs = contexts.get(p.browserContextId);
      contexts.delete(p.browserContextId);
      if (tabs) await Promise.all([...tabs].map((t) => chrome.tabs.remove(t).catch(() => {})));
      if (!tabs) return {};
      const warning = await clearIncognito().catch((e) => `clearing failed: ${e.message}`);
      return warning ? { warning } : {};
    }
    case "Target.createTarget": {
      const ctx = p.browserContextId ? contexts.get(p.browserContextId) : null;
      if (p.browserContextId && !ctx) throw new Error(`unknown browser context ${p.browserContextId}`);
      const incognito = !!ctx;
      const w = await chrome.windows.create({ url: "about:blank", focused: false, incognito, width: 1280, height: 900 });
      const tab = w.tabs?.[0] ?? (await chrome.tabs.query({ windowId: w.id }))[0];
      ours.add(tab.id); ctx?.add(tab.id);
      if (incognito) dirty = true;
      if (incognito && !w.incognito) { await chrome.windows.remove(w.id).catch(() => {}); throw new Error("Chrome did not open an incognito window for the fresh session"); }
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
      dropKeeper();
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
  if (keeper && src.tabId === keeper.tabId) { keeper.attached = false; return; }
  if (src.tabId == null || !sessions.has(sid(src.tabId))) return;
  sessions.delete(sid(src.tabId));
  post({ method: "Target.detachedFromTarget", params: { sessionId: sid(src.tabId), reason } });
});
// a pop-up (target=_blank, window.open) from a tab we drive is ours too
chrome.webNavigation.onCreatedNavigationTarget.addListener(async (d) => {
  if (!ours.has(d.sourceTabId)) return;
  ours.add(d.tabId);
  for (const tabs of contexts.values()) if (tabs.has(d.sourceTabId)) tabs.add(d.tabId);
  try {
    const [targetIdNew, opener] = await Promise.all([targetId(d.tabId), targetId(d.sourceTabId)]);
    post({ method: "Target.targetCreated", params: { targetInfo: { targetId: targetIdNew, type: "page", openerId: opener, url: d.url || "" } } });
  } catch {}
});
// the origins a fresh session loads (pages and frames), for the clear when it closes
chrome.webNavigation.onCommitted.addListener((d) => {
  if (!ours.has(d.tabId) || !/^https?:/.test(d.url)) return;
  for (const tabs of contexts.values()) if (tabs.has(d.tabId)) { visited.add(new URL(d.url).origin); dirty = true; }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === keeper?.tabId) keeper = null;
  if (!ours.has(tabId)) return;
  ours.delete(tabId);
  for (const tabs of contexts.values()) tabs.delete(tabId);
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
