// Talks to the daemon over its Unix socket and starts it when it is not running.
import net from "node:net";
import { spawn } from "node:child_process";
import { openSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { HOME, SOCKET, LOG, PLUGIN_ROOT } from "./paths.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = () => new Promise((res, rej) => { const c = net.connect(SOCKET); c.once("connect", () => res(c)); c.once("error", rej); });

export class Client {
  constructor() { this.sock = null; this.seq = 0; this.pending = new Map(); }

  async ensure() {
    if (this.sock && !this.sock.destroyed) return;
    try { this.sock = await connect(); }
    catch {
      mkdirSync(HOME, { recursive: true, mode: 0o700 });
      const out = openSync(LOG, "a");
      spawn(process.execPath, [join(PLUGIN_ROOT, "src", "daemon.mjs")], { detached: true, stdio: ["ignore", out, out], env: process.env }).unref();
      for (let i = 0; ; i++) {
        await sleep(i < 20 ? 50 : 200);
        try { this.sock = await connect(); break; } catch { if (i > 120) throw new Error(`jevb daemon did not start; see ${LOG}`); }
      }
    }
    let buf = "";
    this.sock.setEncoding("utf8");
    this.sock.on("data", (chunk) => {
      buf += chunk; let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const msg = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
        const p = this.pending.get(msg.id); if (!p) continue;
        this.pending.delete(msg.id);
        msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error));
      }
    });
    this.sock.on("close", () => { for (const p of this.pending.values()) p.reject(new Error("jevb daemon connection closed")); this.pending.clear(); });
    this.sock.on("error", () => {});
  }

  async call(op, session, args = {}) {
    await this.ensure();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.sock.write(JSON.stringify({ id, op, session, args }) + "\n");
    });
  }

  close() { try { this.sock?.end(); } catch {} }
}
