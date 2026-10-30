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
const { CDP, ExtCDP, pairing, pairingCode, EXT_ORIGIN } = await import("../src/cdp.mjs");
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
async function loadSw({ incognito = true } = {}) {
  const windows = new Map(); let nextId = 1;
  const listener = () => ({ addListener() {} });
  globalThis.chrome = {
    extension: { isAllowedIncognitoAccess: async () => incognito },
    windows: {
      create: async (o) => { const id = nextId++, tab = { id: 100 + id, windowId: id }; windows.set(id, { id, incognito: !!o.incognito, tabs: [tab] }); return windows.get(id); },
      getAll: async () => [...windows.values()],
      remove: async (id) => { windows.delete(id); },
    },
    tabs: {
      query: async ({ windowId }) => windows.get(windowId)?.tabs ?? [],
      remove: async (tabId) => { for (const [id, w] of windows) if (w.tabs.some((t) => t.id === tabId)) windows.delete(id); },
      onRemoved: listener(),
    },
    debugger: { getTargets: async () => [...windows.values()].flatMap((w) => w.tabs.map((t) => ({ id: `T${t.id}`, tabId: t.id }))), onEvent: listener(), onDetach: listener(), attach: async () => {}, detach: async () => {} },
    webNavigation: { onCreatedNavigationTarget: listener() },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { getURL: (p) => `chrome-extension://x/${p}`, onMessage: listener(), onStartup: listener(), onInstalled: listener(), getManifest: () => ({ version: "0" }) },
    alarms: { create() {}, onAlarm: listener() },
  };
  globalThis.fetch = async () => ({ ok: false });   // no pair.json: the worker stays unconnected
  const sw = await import(`../extension/sw.js?${Math.random()}`);
  return { sw, windows, incognitoWindows: () => [...windows.values()].filter((w) => w.incognito).length };
}

test("--fresh in the extension: its own incognito window, closed with the session; refused without incognito access", async () => {
  const { sw, windows, incognitoWindows } = await loadSw();
  const send = (method, params = {}) => sw.handle({ id: 1, method, params });
  const a = await send("Target.createBrowserContext");
  assert.match(a.browserContextId, /^incognito-/);
  assert.equal(a.warning, undefined);
  const ta = await send("Target.createTarget", { url: "about:blank", browserContextId: a.browserContextId });
  assert.equal(ta.warning, undefined);
  assert.equal(incognitoWindows(), 1);
  // a session that is not fresh: a normal window
  await send("Target.createTarget", { url: "about:blank" });
  assert.equal([...windows.values()].filter((w) => !w.incognito).length, 1);
  // a second fresh session while the first is open shares the incognito profile: it is said
  const b = await send("Target.createBrowserContext");
  assert.match(b.warning, /shares the incognito profile/);
  await send("Target.disposeBrowserContext", { browserContextId: b.browserContextId });
  // dispose closes the session's incognito window; the next fresh session starts alone
  await send("Target.disposeBrowserContext", { browserContextId: a.browserContextId });
  assert.equal(incognitoWindows(), 0);
  const c = await send("Target.createBrowserContext");
  assert.equal(c.warning, undefined);
  await assert.rejects(send("Target.createTarget", { browserContextId: "incognito-999" }), /unknown browser context/);

  const { sw: noInc } = await loadSw({ incognito: false });
  await assert.rejects(noInc.handle({ id: 1, method: "Target.createBrowserContext", params: {} }), /Allow in Incognito/);
});
