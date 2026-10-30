// The extension front end without Chrome: the daemon's WebSocket server (who may connect, and
// framing), CDP over it as the extension relays it, the pairing file, and the manifest's fixed id.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { listen, refusal } from "../src/wsserver.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
process.env.JEVB_HOME = mkdtempSync(join(tmpdir(), "jevb-ext-"));
const { CDP, ExtCDP, orderedExpr, pairing, pairingCode, EXT_ORIGIN } = await import("../src/cdp.mjs");
const { EXT_ID } = await import("../src/paths.mjs");

const TOKEN = "k".repeat(32);
const open = (port, { origin = EXT_ORIGIN, token = TOKEN, host } = {}) => new Promise((res) => {
  const ws = new WebSocket(`ws://${host ?? "127.0.0.1"}:${port}/jevb?token=${token}`, { headers: { origin } });
  ws.onopen = () => res({ ok: true, ws });
  ws.onerror = () => res({ ok: false });
});
async function server(onConnection = () => {}) {
  const refused = [];
  const s = await listen({ port: 0, origin: EXT_ORIGIN, token: TOKEN, onConnection, onRefuse: (w) => refused.push(w) });
  return { ...s, refused };
}

test("only the extension's origin with the pairing token may connect", async () => {
  const s = await server();
  try {
    assert.equal((await open(s.port)).ok, true);
    assert.equal((await open(s.port, { origin: "https://evil.example" })).ok, false);
    assert.equal((await open(s.port, { origin: "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" })).ok, false);
    assert.equal((await open(s.port, { token: "wrong".repeat(7) })).ok, false);
    assert.equal((await open(s.port, { token: "" })).ok, false);
    assert.deepEqual(s.refused.map((r) => r.split(" ").slice(0, 2).join(" ")), ["bad origin", "bad origin", "bad token", "bad token"]);
  } finally { s.close(); }
});

test("refusal: a foreign Host header (DNS rebinding) is refused", () => {
  const req = (h) => ({ url: `/jevb?token=${TOKEN}`, headers: { host: "127.0.0.1:9", origin: EXT_ORIGIN, upgrade: "websocket", "sec-websocket-key": "x", ...h } });
  assert.equal(refusal(req({}), { origin: EXT_ORIGIN, token: TOKEN, port: 9 }), null);
  assert.equal(refusal(req({ host: "localhost:9" }), { origin: EXT_ORIGIN, token: TOKEN, port: 9 }), null);
  assert.equal(refusal(req({ host: "evil.example:9" }), { origin: EXT_ORIGIN, token: TOKEN, port: 9 }), "bad host");
  assert.equal(refusal(req({ upgrade: "h2c" }), { origin: EXT_ORIGIN, token: TOKEN, port: 9 }), "not a websocket");
});

test("CDP over the extension socket: commands, results, errors, events, large messages", async () => {
  // a fake extension: answers like chrome.debugger would, and pushes one event per command
  let cdp;
  const ready = new Promise((r) => { cdp = r; });
  const s = await server((conn) => cdp(new ExtCDP(conn)));
  const { ws } = await open(s.port);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === "Boom") return ws.send(JSON.stringify({ id: m.id, error: { message: "not available" } }));
    ws.send(JSON.stringify({ method: "Page.frameNavigated", params: { n: m.id }, sessionId: m.sessionId }));
    ws.send(JSON.stringify({ id: m.id, result: { echo: m.params, big: "x".repeat(m.params.big ?? 0) } }));
  };
  const c = await ready;
  try {
    assert.equal(c.transport, "extension");
    const events = [];
    c.on((method, params, sessionId) => events.push([method, params.n, sessionId]));
    assert.deepEqual((await c.send("Runtime.evaluate", { a: 1 }, "ext-7")).echo, { a: 1 });
    assert.equal((await c.send("Page.captureScreenshot", { big: 300_000 }, "ext-7")).big.length, 300_000);
    await assert.rejects(c.send("Boom"), /Boom: not available/);
    assert.deepEqual(events[0], ["Page.frameNavigated", 1, "ext-7"]);
    assert.equal(c.sent, 3);
    ws.close();
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(c.closed, true);
    await assert.rejects(c.send("Runtime.evaluate"), /closed/);
  } finally { s.close(); }
});

test("upload without file access: the file is handed to the page through a DataTransfer", async () => {
  const f = join(process.env.JEVB_HOME, "up.txt"); writeFileSync(f, "hello");
  const sent = [];
  const c = new ExtCDP({ addEventListener() {}, send: (t) => {
    const m = JSON.parse(t); sent.push(m);
    queueMicrotask(() => c.pending.get(m.id)?.[m.method === "DOM.setFileInputFiles" ? "reject" : "resolve"](m.method === "DOM.setFileInputFiles" ? new Error("Not allowed") : { result: { value: 1 } }));
  } });
  await c.send("DOM.setFileInputFiles", { files: [f], objectId: "o1" }, "ext-1");
  assert.deepEqual(sent.map((m) => m.method), ["DOM.setFileInputFiles", "Runtime.callFunctionOn"]);
  assert.deepEqual(sent[1].params.arguments[0].value, [{ name: "up.txt", data: Buffer.from("hello").toString("base64") }]);
  assert.equal(sent[1].params.objectId, "o1");
});

test("pairing: one random token in a 0600 file, stable across calls", () => {
  const a = pairing(), b = pairing();
  assert.equal(a.token, b.token);
  assert.ok(a.token.length >= 30);
  assert.match(pairingCode(a), /^jevb:\d+:[A-Za-z0-9_-]{30,}$/);
  assert.equal(statSync(join(process.env.JEVB_HOME, "ext-pairing.json")).mode & 0o777, 0o600);
});

test("the manifest key gives the extension id the daemon checks", () => {
  const m = JSON.parse(readFileSync(join(HERE, "..", "extension", "manifest.json"), "utf8"));
  const hex = createHash("sha256").update(Buffer.from(m.key, "base64")).digest("hex").slice(0, 32);
  assert.equal([...hex].map((x) => String.fromCharCode(97 + parseInt(x, 16))).join(""), EXT_ID);
  assert.equal(m.manifest_version, 3);
  assert.ok(m.permissions.includes("debugger"));
  assert.equal(EXT_ORIGIN, `chrome-extension://${EXT_ID}`);
  assert.ok(CDP);
});

// sw.js against a stub chrome.*: what a --fresh session gets from the extension
async function loadSw({ incognito = true, pairJson = null } = {}) {
  const windows = new Map(); let nextId = 1; const cmds = [], committed = [];
  const listener = (into) => ({ addListener(f) { into?.push(f); } });
  const allTabs = () => [...windows.values()].flatMap((w) => w.tabs);
  globalThis.chrome = {
    extension: { isAllowedIncognitoAccess: async () => incognito },
    windows: {
      create: async (o) => { const id = nextId++, tab = { id: 100 + id, windowId: id, incognito: !!o.incognito }; windows.set(id, { id, incognito: !!o.incognito, state: o.state, tabs: [tab] }); return windows.get(id); },
      getAll: async () => [...windows.values()],
      remove: async (id) => { windows.delete(id); },
    },
    tabs: {
      get: async (tabId) => { const t = allTabs().find((x) => x.id === tabId); if (!t) throw new Error("no tab"); return t; },
      query: async ({ windowId } = {}) => (windowId == null ? allTabs() : windows.get(windowId)?.tabs ?? []),
      remove: async (tabId) => { for (const [id, w] of windows) if (w.tabs.some((t) => t.id === tabId)) windows.delete(id); },
      onRemoved: listener(),
    },
    debugger: {
      getTargets: async () => allTabs().map((t) => ({ id: `T${t.id}`, tabId: t.id })), onEvent: listener(), onDetach: listener(),
      attach: async () => {}, detach: async () => {},
      sendCommand: async (target, method, params) => { cmds.push({ tabId: target.tabId, method, params }); return {}; },
    },
    webNavigation: { onCreatedNavigationTarget: listener(), onCommitted: listener(committed) },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { getURL: (p) => `chrome-extension://x/${p}`, onMessage: listener(), onStartup: listener(), onInstalled: listener(), getManifest: () => ({ version: "0" }) },
    alarms: { create() {}, onAlarm: listener() },
  };
  // no pair.json: the worker stays unconnected (a test install's pair.json is given as pairJson)
  globalThis.fetch = async () => (pairJson ? { ok: true, json: async () => pairJson } : { ok: false });
  globalThis.WebSocket = class { constructor() { this.readyState = 0; } close() {} };
  const sw = await import(`../extension/sw.js?${Math.random()}`);
  await new Promise((r) => setTimeout(r, 0));   // the worker reads its pairing at load
  const navigate = (targetId, url) => committed.forEach((f) => f({ tabId: Number(targetId.slice(1)), frameId: 0, url }));
  const clears = () => cmds.filter((c) => c.method === "Network.clearBrowserCookies").length;
  return { sw, windows, cmds, clears, navigate, incognitoWindows: () => [...windows.values()].filter((w) => w.incognito).length };
}

test("--fresh: its own incognito window, joined to a kept incognito profile; refused without incognito access", async () => {
  const { sw, windows, incognitoWindows } = await loadSw();
  const send = (method, params = {}) => sw.handle({ id: 1, method, params });
  const a = await send("Target.createBrowserContext");
  assert.match(a.browserContextId, /^incognito-/);
  assert.equal(a.isolate, "incognito");
  assert.equal(a.warning, undefined);
  // the keeper: one blank, minimized incognito window that holds the incognito profile open
  assert.equal(incognitoWindows(), 1);
  assert.equal([...windows.values()][0].state, "minimized");
  const ta = await send("Target.createTarget", { url: "about:blank", browserContextId: a.browserContextId });
  assert.equal(incognitoWindows(), 2);
  // a session that is not fresh: a normal window
  await send("Target.createTarget", { url: "about:blank" });
  assert.equal([...windows.values()].filter((w) => !w.incognito).length, 1);
  // the session's window closes with it; the keeper stays, so the next session needs no new profile
  await send("Target.closeTarget", { targetId: ta.targetId });
  await send("Target.disposeBrowserContext", { browserContextId: a.browserContextId });
  assert.equal(incognitoWindows(), 1);
  await send("Target.createBrowserContext");
  assert.equal(incognitoWindows(), 1);
  await assert.rejects(send("Target.createTarget", { browserContextId: "incognito-999" }), /unknown browser context/);
  // the daemon stops: the keeper goes too
  await send("Browser.close");
  assert.equal(incognitoWindows(), 0);

  const { sw: noInc } = await loadSw({ incognito: false });
  await assert.rejects(noInc.handle({ id: 1, method: "Target.createBrowserContext", params: {} }), /Allow in Incognito/);
});

test("--fresh: closing a session clears the incognito cookies and the storage of every origin it visited (HTTP cache kept)", async () => {
  const { sw, cmds, clears, navigate } = await loadSw({ pairJson: { port: 1, token: "x".repeat(24) } });
  const send = (method, params = {}) => sw.handle({ id: 1, method, params });
  const a = await send("Target.createBrowserContext");
  assert.equal(clears(), 0);                          // nothing ran in the incognito profile yet
  const ta = await send("Target.createTarget", { url: "about:blank", browserContextId: a.browserContextId });
  navigate(ta.targetId, "https://shop.example/login");
  navigate(ta.targetId, "https://cdn.example/frame");
  navigate(ta.targetId, "about:blank");
  await send("Target.closeTarget", { targetId: ta.targetId });
  assert.deepEqual(await send("Target.disposeBrowserContext", { browserContextId: a.browserContextId }), {});
  assert.equal(clears(), 1);
  const storage = cmds.filter((c) => c.method === "Storage.clearDataForOrigin");
  assert.deepEqual(storage.map((c) => c.params.origin).sort(), ["https://cdn.example", "https://shop.example"]);
  assert.ok(storage.every((c) => c.params.storageTypes === "all"));
  // on the keeper tab, never on a tab of your own; no HTTP-cache clear anywhere
  const keeperTab = cmds[0].tabId;
  assert.ok(cmds.every((c) => c.tabId === keeperTab));
  assert.ok(!cmds.some((c) => /clearBrowserCache/.test(c.method)));
  // the next fresh session starts clean already: no clear on the open path
  const b = await send("Target.createBrowserContext");
  assert.equal(clears(), 1);
  const tb = await send("Target.createTarget", { url: "about:blank", browserContextId: b.browserContextId });
  navigate(tb.targetId, "https://shop.example/");
  // a fresh session while another is open: nothing is cleared under it, and it is said
  const c = await send("Target.createBrowserContext");
  assert.match(c.warning, /another fresh session is open/);
  const d = await send("Target.disposeBrowserContext", { browserContextId: c.browserContextId });
  assert.match(d.warning, /another fresh session is open/);
  assert.equal(clears(), 1);
  // the last one closes: cleared
  await send("Target.disposeBrowserContext", { browserContextId: b.browserContextId });
  assert.equal(clears(), 2);
});

test("--fresh never clears while you have incognito tabs of your own", async () => {
  const { sw, windows, clears, navigate } = await loadSw();
  const send = (method, params = {}) => sw.handle({ id: 1, method, params });
  const a = await send("Target.createBrowserContext");
  const ta = await send("Target.createTarget", { url: "about:blank", browserContextId: a.browserContextId });
  navigate(ta.targetId, "https://mail.example/");
  windows.set(99, { id: 99, incognito: true, tabs: [{ id: 999, windowId: 99, incognito: true }] });   // yours
  const r = await send("Target.disposeBrowserContext", { browserContextId: a.browserContextId });
  assert.match(r.warning, /incognito tab\(s\) of your own/);
  assert.equal(clears(), 0);
  // it stays owed: the next fresh session says it shares, then clears once yours are gone
  const b = await send("Target.createBrowserContext");
  assert.match(b.warning, /shares cookies and storage/);
  windows.delete(99);
  await send("Target.disposeBrowserContext", { browserContextId: b.browserContextId });
  assert.equal(clears(), 1);
});

// chrome.debugger hands back results with sorted keys; Jev reads key order, so by-value evaluates
// cross as JSON text and keep the page's order
test("extension transport keeps the key order of evaluate results", async () => {
  const sortKeys = (v, seen = new Set()) => {
    if (!v || typeof v !== "object" || seen.has(v)) return seen.has(v) ? {} : v;
    seen.add(v);
    return Array.isArray(v) ? v.map((x) => sortKeys(x, seen)) : Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k], seen)]));
  };
  const sent = [];
  const fake = { listeners: {}, addEventListener(t, f) { (this.listeners[t] ??= []).push(f); }, close() {},
    async send(raw) {
      const m = JSON.parse(raw); sent.push(m);
      // "the page": run the expression here, then sort the result the way Chrome does
      let result, exceptionDetails;
      try { const v = await (0, eval)(m.params.expression); result = v === undefined ? { type: "undefined" } : { type: typeof v, value: sortKeys(v) }; }
      catch (e) { exceptionDetails = { text: String(e) }; }
      for (const f of this.listeners.message) f({ data: JSON.stringify({ id: m.id, result: { result, ...(exceptionDetails ? { exceptionDetails } : {}) } }) });
    } };
  const cdp = new ExtCDP(fake);
  const ev = (expression, awaitPromise = true) => cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
  const r = await ev("({ i: 1, tag: 'a', text: 'x', near: 'y', list: [{ z: 1, a: 2 }] })");
  assert.deepEqual(Object.keys(r.result.value), ["i", "tag", "text", "near", "list"]);
  assert.deepEqual(Object.keys(r.result.value.list[0]), ["z", "a"]);
  assert.equal((await ev("Promise.resolve('s')")).result.value, "s");
  assert.equal((await ev("1 + 1 // a trailing comment", false)).result.value, 2);
  assert.equal((await ev("undefined")).result.type, "undefined");
  assert.match((await ev("(() => { throw new Error('boom') })()")).exceptionDetails.text, /boom/);
  // statements are not an expression: sent as they are (order is then Chrome's)
  const before = sent.length;
  assert.equal((await ev("var q = 3; q * 2;")).result.value, 6);
  assert.equal(sent[before].params.expression, "var q = 3; q * 2;");
  // a value JSON cannot carry (a cycle): read again by value, never run twice
  globalThis.runs = 0;
  const cyc = await ev("(() => { globalThis.runs++; const o = { b: 1 }; o.self = o; return o })()");
  assert.equal(globalThis.runs, 1);
  assert.equal(cyc.result.value.b, 1);
  assert.equal(orderedExpr("a; b", true), null);
});
