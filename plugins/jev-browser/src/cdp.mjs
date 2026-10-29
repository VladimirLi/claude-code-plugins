// Raw CDP over one WebSocket (Node's built-in client), and the dedicated Chrome it talks to.
// No Playwright: only the few domains we need, and none of its fixed waits.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import net from "node:net";
import { CHROME, PROFILE, USER_CHROME } from "./paths.mjs";

const SLOW = Number(process.env.JEVB_DEBUG_SLOW) || 0;
const SLOW_LOG = (s) => process.stderr.write(`[slow] ${s}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class CDP {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.pending = new Map(); this.handlers = new Set(); this.closed = false;
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
