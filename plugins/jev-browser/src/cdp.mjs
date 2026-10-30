// Raw CDP over one WebSocket (Node's built-in client), and the dedicated Chrome it talks to.
// No Playwright: only the few domains we need, and none of its fixed waits.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, chmodSync, cpSync, appendFileSync } from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import net from "node:net";
import { CHROME, PROFILE, USER_CHROME, HOME, LOG, PLUGIN_ROOT, EXT_TEST, EXT_ID, EXT_PORT } from "./paths.mjs";
import { listen } from "./wsserver.mjs";

const SLOW = Number(process.env.JEVB_DEBUG_SLOW) || 0;
const SLOW_LOG = (s) => process.stderr.write(`[slow] ${s}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class CDP {
  // transport: "cdp" (Chrome's own DevTools WebSocket) or "extension" (the jev-browser extension's
  // WebSocket to the daemon, which relays to chrome.debugger). sent counts commands, for the log.
  constructor(ws, transport = "cdp") {
    this.ws = ws; this.transport = transport; this.sent = 0; this.seq = 0; this.pending = new Map(); this.handlers = new Set(); this.closed = false;
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : Buffer.from(ev.data).toString());
      if (msg.id != null) {
        const p = this.pending.get(msg.id); if (!p) return;
        this.pending.delete(msg.id);
        if (msg.error) p.reject(Object.assign(new Error(`${p.method}: ${msg.error.message}`), { cdp: msg.error }));
        else p.resolve(msg.result);
      } else for (const h of this.handlers) { try { h(msg.method, msg.params, msg.sessionId); } catch {} }
    });
    ws.addEventListener("close", () => {
      this.closed = true;
      for (const p of this.pending.values()) p.reject(new Error("browser connection closed"));
      this.pending.clear();
    });
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.addEventListener("open", res, { once: true }); ws.addEventListener("error", () => rej(new Error("cannot connect to Chrome")), { once: true }); });
    return new CDP(ws);
  }

  send(method, params = {}, sessionId, timeout = 30_000) {
    if (this.closed) return Promise.reject(new Error("browser connection closed"));
    const id = ++this.seq;
    this.sent++;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method}: timed out after ${timeout} ms`)); }, timeout);
      const t0 = Date.now();
      const done = () => { clearTimeout(t); const ms = Date.now() - t0; if (SLOW && ms > SLOW) SLOW_LOG(`${method} ${ms}ms`); };
      this.pending.set(id, { method, resolve: (v) => { done(); resolve(v); }, reject: (e) => { done(); reject(e); } });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }

  on(fn) { this.handlers.add(fn); return () => this.handlers.delete(fn); }
  close() { try { this.ws.close(); } catch {} }
}

// Attach to the user's own running Chrome. Chrome writes DevToolsActivePort into its profile dir
// once "Allow remote debugging" is on at chrome://inspect/#remote-debugging, and asks the user to
// Allow each new connection (Chrome 144+), so the open waits without a deadline of its own.
export async function attachUserChrome() {
  const portFile = join(USER_CHROME, "DevToolsActivePort");
  const how = "In your Chrome open chrome://inspect/#remote-debugging and switch on \"Allow remote debugging for this browser instance\"";
  if (!existsSync(portFile)) throw new Error(`Your Chrome does not allow remote debugging. ${how}.`);
  const [port, path] = readFileSync(portFile, "utf8").split("\n").map((s) => s.trim());
  const live = await new Promise((res) => { const c = net.connect(Number(port), "127.0.0.1", () => { c.end(); res(true); }); c.on("error", () => res(false)); });
  if (!live) throw new Error(`Your Chrome is not running with remote debugging on. Start Chrome; if this repeats: ${how}.`);
  const ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", () => rej(new Error("Chrome refused the connection (was Allow clicked in Chrome's prompt?)")), { once: true });
  });
  return { cdp: new CDP(ws), proc: null };
}

// Launch Chrome with our own profile (never the user's), or reuse one already running on it.
// Returns { cdp, proc }.
export async function launchChrome({ headed = /^(1|true)$/i.test(process.env.JEVB_HEADED || "") } = {}) {
  mkdirSync(PROFILE, { recursive: true });
  const portFile = join(PROFILE, "DevToolsActivePort");
  // A Chrome left over from an earlier daemon on this profile: attach to it.
  if (existsSync(portFile)) {
    const [port, path] = readFileSync(portFile, "utf8").split("\n");
    try { return { cdp: await CDP.connect(`ws://127.0.0.1:${port}${path}`), proc: null }; }
    catch { rmSync(portFile, { force: true }); }
  }
  if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME} (set JEVB_CHROME)`);
  const args = [
    `--user-data-dir=${PROFILE}`, "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--use-mock-keychain", "--password-store=basic", "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--disable-blink-features=AutomationControlled", "--window-size=1280,900", "--lang=en-US",
    // lean: no background services, fewer processes (the Mac running this is often busy)
    "--disable-extensions", "--disable-component-update", "--disable-sync", "--disable-background-networking",
    "--disable-default-apps", "--mute-audio", "--no-pings", "--disable-breakpad", "--disable-hang-monitor",
    "--disable-ipc-flooding-protection", "--disable-client-side-phishing-detection", "--metrics-recording-only",
    "--renderer-process-limit=6",
    ...(headed ? [] : ["--headless"]), "about:blank",
  ];
  const proc = spawn(CHROME, args, { stdio: "ignore", detached: false });
  for (let i = 0; i < 400; i++) {
    await sleep(100);
    if (proc.exitCode != null) throw new Error(`Chrome exited at start (code ${proc.exitCode})`);
    if (!existsSync(portFile)) continue;
    const [port, path] = readFileSync(portFile, "utf8").split("\n");
    if (!port || !path) continue;
    try { return { cdp: await CDP.connect(`ws://127.0.0.1:${port.trim()}${path.trim()}`), proc }; } catch {}
  }
  proc.kill();
  throw new Error("Chrome did not open its DevTools port within 40 s");
}

// ------------------------------------------------------------------ extension transport
// The jev-browser extension (extension/) connects to the daemon's WebSocket on 127.0.0.1 and
// relays CDP to chrome.debugger, attached per tab. No remote-debugging port, no chrome://inspect
// switch. Only the extension may connect: its Origin (chrome-extension://<EXT_ID>, fixed by the
// manifest key) and the pairing token from a 0600 file, given to the extension once.
const extLog = (s) => { try { appendFileSync(LOG, `${new Date().toISOString()} ext: ${s}\n`); } catch {} };
export const EXT_ORIGIN = `chrome-extension://${EXT_ID}`;
const PAIRING = join(HOME, "ext-pairing.json");

// The pairing ({port, token}) for the extension in your Chrome: made once, kept in a 0600 file.
export function pairing() {
  try { const p = JSON.parse(readFileSync(PAIRING, "utf8")); if (p.token && p.port) return p; } catch {}
  mkdirSync(HOME, { recursive: true, mode: 0o700 });
  const p = { port: EXT_PORT, token: randomBytes(24).toString("base64url") };
  writeFileSync(PAIRING, JSON.stringify(p), { mode: 0o600 }); chmodSync(PAIRING, 0o600);
  return p;
}
export const pairingCode = (p = pairing()) => `jevb:${p.port}:${p.token}`;

// The extension's CDP: DOM.setFileInputFiles needs "Allow access to file URLs" for an extension,
// so without it the file is handed to the page instead (read here, set through a DataTransfer).
export class ExtCDP extends CDP {
  constructor(ws) { super(ws, "extension"); }
  async send(method, params = {}, sessionId, timeout) {
    if (method === "Target.createBrowserContext" || (method === "Target.createTarget" && params.browserContextId)) {
      // a fresh session: the extension gives it its own incognito window, or refuses
      const r = await super.send(method, params, sessionId, timeout).catch((e) => { extLog(`fresh session refused: ${e.message}`); throw e; });
      if (r.browserContextId) extLog(`fresh session context ${r.browserContextId} (incognito window, empty cookies and storage)`);
      if (r.warning) extLog(`warning: fresh session ${params.browserContextId ?? r.browserContextId ?? ""} ${r.warning}`);
      return r;
    }
    if (method !== "DOM.setFileInputFiles" || !params.objectId) return super.send(method, params, sessionId, timeout);
    try { return await super.send(method, params, sessionId, timeout); }
    catch (e) {
      if (!/not allowed|file access|permission/i.test(e.message)) throw e;
      const files = (params.files || []).map((f) => ({ name: f.split("/").pop(), data: readFileSync(f).toString("base64") }));
      const fn = `function (files) {
        const dt = new DataTransfer();
        for (const f of files) dt.items.add(new File([Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0))], f.name));
        this.files = dt.files; return this.files.length;
      }`;
      const r = await super.send("Runtime.callFunctionOn", { objectId: params.objectId, functionDeclaration: fn, arguments: [{ value: files }], returnByValue: true }, sessionId, timeout);
      if (r.exceptionDetails) throw new Error(`upload: ${r.exceptionDetails.text}`);
      return {};
    }
  }
}

// One WebSocket server per daemon; every (re)connection of the extension becomes the current
// ExtCDP. A second client with the right origin and token replaces the first (the extension's
// service worker reconnecting); any other client is refused.
let hub = null;
async function extHub({ port, token }) {
  if (hub) return hub;
  const h = { cdp: null, waiters: new Set(), refused: 0, token };
  const { port: bound } = await listen({
    port, token, origin: EXT_ORIGIN,
    onConnection(conn) {
      const prev = h.cdp; h.cdp = new ExtCDP(conn);
      if (prev && !prev.closed) prev.close();
      extLog(`extension connected on 127.0.0.1:${h.port}`);
      for (const w of h.waiters) w(h.cdp);
      h.waiters.clear();
    },
    onRefuse(why) { h.refused++; extLog(`refused a client: ${why}`); },
  });
  h.port = bound;
  h.next = (ms, how) => (h.cdp && !h.cdp.closed ? Promise.resolve(h.cdp) : new Promise((res, rej) => {
    const t = setTimeout(() => { h.waiters.delete(res); rej(new Error(how)); }, ms);
    h.waiters.add((c) => { clearTimeout(t); res(c); });
  }));
  return (hub = h);
}

// The extension front end. Test mode (JEVB_EXT_TEST=1): launch a dedicated headless Chrome with
// its own profile under HOME that loads the unpacked extension. Branded Chrome ignores
// --load-extension, so the extension is installed with CDP Extensions.loadUnpacked over
// --remote-debugging-pipe; that pipe carries that one command and nothing else, and every
// later command goes daemon -> WebSocket -> extension -> chrome.debugger.
// Otherwise: wait for the extension in your own Chrome to connect. Returns { cdp, proc }.
export async function connectExtension() {
  if (EXT_TEST) {
    const h = await extHub({ port: 0, token: randomBytes(24).toString("base64url") });
    if (h.cdp && !h.cdp.closed) return { cdp: h.cdp, proc: h.proc ?? null };
    const proc = await launchTestChrome(h);
    h.proc = proc;
    const cdp = await h.next(30_000, "the jev-browser extension did not connect to the daemon within 30 s");
    return { cdp, proc };
  }
  const p = pairing();
  const h = await extHub(p);
  const cdp = await h.next(60_000, `The jev-browser extension is not connected. Load it unpacked from ${join(PLUGIN_ROOT, "extension")} at chrome://extensions (Developer mode), click its toolbar icon and paste the code from \`jevb pair\`. Chrome must be running.`);
  return { cdp, proc: null };
}

async function launchTestChrome(h) {
  if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME} (set JEVB_CHROME)`);
  const profile = join(HOME, "ext-test-profile"), dir = join(HOME, "ext-test", "extension");
  // a clean profile each launch (each --fresh session also gets its own incognito profile), and a copy of the extension
  // that carries this launch's pairing
  rmSync(profile, { recursive: true, force: true }); mkdirSync(profile, { recursive: true, mode: 0o700 });
  rmSync(dir, { recursive: true, force: true }); mkdirSync(dir, { recursive: true, mode: 0o700 });
  cpSync(join(PLUGIN_ROOT, "extension"), dir, { recursive: true });
  writeFileSync(join(dir, "pair.json"), JSON.stringify({ port: h.port, token: h.token }), { mode: 0o600 });
  const args = [
    `--user-data-dir=${profile}`, "--remote-debugging-pipe", "--enable-unsafe-extension-debugging",
    "--no-first-run", "--no-default-browser-check", "--use-mock-keychain", "--password-store=basic",
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
    "--disable-features=Translate,MediaRouter,OptimizationHints", "--disable-blink-features=AutomationControlled",
    "--window-size=1280,900", "--lang=en-US", "--disable-component-update", "--disable-sync", "--disable-background-networking",
    "--disable-default-apps", "--mute-audio", "--no-pings", "--disable-breakpad", "--disable-hang-monitor",
    "--disable-ipc-flooding-protection", "--disable-client-side-phishing-detection", "--metrics-recording-only",
    "--renderer-process-limit=6", "--headless", "about:blank",
  ];
  const proc = spawn(CHROME, args, { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"], detached: false });
  const [toChrome, fromChrome] = [proc.stdio[3], proc.stdio[4]];
  const reply = new Promise((res, rej) => {
    let buf = "";
    fromChrome.on("data", (d) => {
      buf += d.toString("utf8"); let i;
      while ((i = buf.indexOf("\0")) >= 0) {
        const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
        if (m.id === 1) m.error ? rej(new Error(`Extensions.loadUnpacked: ${m.error.message}`)) : res(m.result);
      }
    });
    proc.once("exit", (code) => rej(new Error(`Chrome exited at start (code ${code})`)));
    setTimeout(() => rej(new Error("Chrome did not answer on the pipe within 30 s")), 30_000);
  });
  // allowed in incognito, so that each --fresh session gets an empty incognito profile
  toChrome.write(JSON.stringify({ id: 1, method: "Extensions.loadUnpacked", params: { path: dir, enableInIncognito: true } }) + "\0");
  let r;
  try { r = await reply; } catch (e) { proc.kill(); throw e; }
  // keep the pipe open (closing it ends Chrome) but never send on it again
  fromChrome.removeAllListeners("data"); fromChrome.on("data", () => {});
  if (r.id !== EXT_ID) extLog(`loaded extension id ${r.id}, expected ${EXT_ID}`);
  extLog(`test Chrome ${proc.pid}: pipe sent 1 command (Extensions.loadUnpacked ${r.id}, incognito allowed); all others go over the extension`);
  return proc;
}
