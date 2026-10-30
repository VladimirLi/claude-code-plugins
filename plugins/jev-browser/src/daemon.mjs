#!/usr/bin/env node
// The long-lived browser daemon. It launches the dedicated Chrome at start (kept warm), holds
// the one CDP connection, and serves the MCP server and the jevb CLI over a Unix socket that
// only this user can open. One session name = one tab; sessions run in parallel.
import net from "node:net";
import { mkdirSync, rmSync, chmodSync, appendFileSync, existsSync } from "node:fs";
import { HOME, SOCKET, LOG, ATTACH } from "./paths.mjs";
import { launchChrome, attachUserChrome } from "./cdp.mjs";
import { Browser } from "./tab.mjs";
import { Agent } from "./agent.mjs";
import { stopHaiku } from "./haiku.mjs";

const IDLE_MS = (Number(process.env.JEVB_IDLE_HOURS) || 8) * 3600_000;
mkdirSync(HOME, { recursive: true, mode: 0o700 });
const log = (s) => { try { appendFileSync(LOG, `${new Date().toISOString()} ${s}\n`); } catch {} };

let browser = null, starting = null, proc = null;
const agents = new Map();

async function getBrowser() {
  if (browser?.alive) return browser;
  starting ??= (async () => {
    const t0 = Date.now();
    const { cdp, proc: p } = ATTACH ? await attachUserChrome() : await launchChrome();
    proc = p;
    const b = new Browser(cdp, p, { user: ATTACH });
    await b.init();
    agents.clear();
    browser = b;
    log(`chrome ready in ${Date.now() - t0} ms (${ATTACH ? "your Chrome" : p ? "launched" : "attached"})`);
    return b;
  })().finally(() => { starting = null; });
  return starting;
}

async function agentFor(session, fresh) {
  const b = await getBrowser();
  const tab = await b.tab(session, { fresh });
  let a = agents.get(session);
  if (!a || a.tab !== tab) { a = new Agent(tab); agents.set(session, a); }
  return a;
}

const OPS = {
  ping: async () => ({ text: "pong", data: { pid: process.pid } }),
  sessions: async () => { const b = await getBrowser(); const names = [...b.tabs.keys()]; return { text: names.join("\n") || "(no sessions)", data: { sessions: names } }; },
  close: async (session) => { const b = await getBrowser(); agents.delete(session); const ok = await b.close(session); return { text: ok ? `closed ${session}` : `no session ${session}`, data: { ok } }; },
  shutdown: async () => { setTimeout(() => quit(0), 50); return { text: "stopping" }; },
};
for (const op of ["open", "do", "act", "observe", "check", "eval"]) {
  OPS[op] = async (session, args) => {
    const a = await agentFor(session, !!args.fresh);
    return a.tab.run(() => (op === "eval" ? a.evalJs(args) : a[op](args)));
  };
}

let idle;
const touch = () => { clearTimeout(idle); idle = setTimeout(() => quit(0), IDLE_MS); };

async function handle(req) {
  const t0 = Date.now();
  touch();
  const fn = OPS[req.op];
  if (!fn) return { id: req.id, ok: false, error: `unknown op ${req.op}` };
  try {
    const result = await fn(req.session || "default", req.args || {});
    log(`${req.session || "default"} ${req.op} ${Date.now() - t0}ms ${result?.data?.status ?? "ok"}`);
    return { id: req.id, ok: true, result };
  } catch (e) {
    log(`${req.session || "default"} ${req.op} ${Date.now() - t0}ms error ${String(e.message).slice(0, 120)}`);
    return { id: req.id, ok: false, error: String(e.message) };
  }
}

async function quit(code) {
  try { server.close(); } catch {}
  try { stopHaiku(); } catch {}
  try { rmSync(SOCKET, { force: true }); } catch {}
  // The user's own Chrome is never closed: only the tabs this daemon opened.
  const closing = ATTACH ? Promise.all([...(browser?.tabs.keys() ?? [])].map((n) => browser.close(n))) : browser?.cdp.send("Browser.close");
  try { await Promise.race([closing, new Promise((r) => setTimeout(r, 3000))]); } catch {}
  try { browser?.cdp.close(); } catch {}
  if (proc && proc.exitCode == null) try { proc.kill(); } catch {}
  process.exit(code);
}

// A socket file left by a crashed daemon: remove it only if nobody answers on it.
if (existsSync(SOCKET)) {
  const alive = await new Promise((res) => { const c = net.connect(SOCKET, () => { c.end(); res(true); }); c.on("error", () => res(false)); });
  if (alive) { console.error("jevb daemon already running"); process.exit(0); }
  rmSync(SOCKET, { force: true });
}

const server = net.createServer((sock) => {
  let buf = "";
  sock.setEncoding("utf8");
  sock.on("data", (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      let req; try { req = JSON.parse(line); } catch { continue; }
      handle(req).then((res) => { if (!sock.destroyed) sock.write(JSON.stringify(res) + "\n"); });
    }
  });
  sock.on("error", () => {});
});
server.listen(SOCKET, () => { chmodSync(SOCKET, 0o600); log(`daemon ${process.pid} listening`); });
process.on("SIGTERM", () => quit(0));
process.on("SIGINT", () => quit(0));
process.on("unhandledRejection", (e) => log(`unhandled: ${String(e?.message ?? e).slice(0, 200)}`));
touch();
// warm at start, not at first open (your own Chrome is attached on first use: that may show Allow)
if (!ATTACH) getBrowser().catch((e) => log(`chrome start failed: ${e.message}`));
