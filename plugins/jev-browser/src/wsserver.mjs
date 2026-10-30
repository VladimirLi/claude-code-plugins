// A minimal WebSocket server (RFC 6455, text frames) on 127.0.0.1, for the extension front end.
// Node has a WebSocket client but no server, and the plugin takes no npm dependencies.
// Each connection looks like a browser WebSocket to the CDP class: addEventListener("message" |
// "close"), send(string), close(), so the same request/response code serves both transports.
import http from "node:http";
import { createHash, timingSafeEqual } from "node:crypto";

const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const MAX_MESSAGE = 64 * 1024 * 1024;

export class WSConn {
  constructor(socket) {
    this.socket = socket; this.listeners = { message: new Set(), close: new Set() };
    this.buf = Buffer.alloc(0); this.parts = []; this.closed = false;
    socket.setNoDelay(true);
    socket.on("data", (d) => { this.buf = this.buf.length ? Buffer.concat([this.buf, d]) : d; this.parse(); });
    socket.on("close", () => this.end());
    socket.on("error", () => this.end());
  }
  addEventListener(type, fn) { this.listeners[type]?.add(fn); }
  emit(type, ev) { for (const fn of this.listeners[type] ?? []) { try { fn(ev); } catch {} } }
  end() { if (this.closed) return; this.closed = true; try { this.socket.destroy(); } catch {} this.emit("close", {}); }

  parse() {
    for (;;) {
      const b = this.buf;
      if (b.length < 2) return;
      const fin = (b[0] & 0x80) !== 0, op = b[0] & 0x0f, masked = (b[1] & 0x80) !== 0;
      let len = b[1] & 0x7f, at = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); at = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); at = 10; }
      if (len > MAX_MESSAGE) return this.end();
      if (!masked) return this.end();   // a client must mask its frames
      if (b.length < at + 4 + len) return;
      const mask = b.subarray(at, at + 4), data = Buffer.from(b.subarray(at + 4, at + 4 + len));
      for (let i = 0; i < data.length; i++) data[i] ^= mask[i & 3];
      this.buf = b.subarray(at + 4 + len);
      if (op === 0x8) { this.frame(0x8, Buffer.alloc(0)); return this.end(); }
      if (op === 0x9) { this.frame(0xa, data); continue; }
      if (op === 0xa) continue;
      if (op === 0x1 || op === 0x2 || op === 0x0) {
        this.parts.push(data);
        if (this.parts.reduce((n, p) => n + p.length, 0) > MAX_MESSAGE) return this.end();
        if (fin) { const msg = Buffer.concat(this.parts).toString("utf8"); this.parts = []; this.emit("message", { data: msg }); }
      }
    }
  }
  frame(op, payload) {
    if (this.closed) return;
    const n = payload.length;
    const head = n < 126 ? Buffer.from([0x80 | op, n]) : n < 65536 ? Buffer.from([0x80 | op, 126, n >> 8, n & 255]) : Buffer.concat([Buffer.from([0x80 | op, 127]), (() => { const x = Buffer.alloc(8); x.writeBigUInt64BE(BigInt(n)); return x; })()]);
    this.socket.write(Buffer.concat([head, payload]));
  }
  send(text) { this.frame(0x1, Buffer.from(text, "utf8")); }
  close() { this.frame(0x8, Buffer.alloc(0)); this.end(); }
}

export const sameToken = (a, b) => {
  const x = Buffer.from(String(a ?? "")), y = Buffer.from(String(b ?? ""));
  return x.length === y.length && x.length > 0 && timingSafeEqual(x, y);
};

// Who may connect: only `origin` (chrome-extension://<id>), presenting `token` in the URL
// (?token=...), with a Host of 127.0.0.1 or localhost on our port (no DNS rebinding).
// Returns null when allowed, else the reason for the refusal.
export function refusal(req, { origin, token, port }) {
  const host = String(req.headers.host || "");
  if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) return "bad host";
  if (req.headers.origin !== origin) return `bad origin ${String(req.headers.origin || "(none)").slice(0, 80)}`;
  if (String(req.headers.upgrade || "").toLowerCase() !== "websocket" || !req.headers["sec-websocket-key"]) return "not a websocket";
  let t = null;
  try { t = new URL(req.url, "http://x").searchParams.get("token"); } catch {}
  if (!sameToken(t, token)) return "bad token";
  return null;
}

// Listen on 127.0.0.1:port (0 = any free port). onConnection(conn) gets each accepted client;
// onRefuse(reason) each refused one. Resolves to { server, port, close }; close() also ends
// the live connections.
export function listen({ port = 0, origin, token, onConnection, onRefuse = () => {} }) {
  const server = http.createServer((req, res) => { res.writeHead(404); res.end(); });
  const conns = new Set();
  server.on("upgrade", (req, socket) => {
    const why = refusal(req, { origin, token, port: server.address().port });
    if (why) { onRefuse(why); socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return; }
    const accept = createHash("sha1").update(req.headers["sec-websocket-key"] + GUID).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const c = new WSConn(socket);
    conns.add(c); c.addEventListener("close", () => conns.delete(c));
    onConnection(c);
  });
  return new Promise((res, rej) => {
    server.once("error", rej);
    server.listen(port, "127.0.0.1", () => res({ server, port: server.address().port, close: () => { server.close(); for (const c of conns) c.end(); } }));
  });
}
