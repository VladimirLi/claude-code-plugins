// Where the daemon keeps its state, and where the Jev key comes from.
// The CLI and the MCP server must agree on HOME, so neither uses CLAUDE_PLUGIN_DATA
// (only the MCP server would see it). JEVB_HOME overrides both.
import { homedir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const HOME = process.env.JEVB_HOME || join(homedir(), ".claude", "plugins", "data", "jev-browser-vladimirli-plugins");
export const PROFILE = join(HOME, "chrome-profile");
export const LOG = join(HOME, "daemon.log");
export const SHOTS = join(HOME, "shots");
export const CHROME = process.env.JEVB_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
// Attach mode: drive the user's own running Chrome (its tabs, logins and extensions) instead of
// launching the dedicated one. Needs chrome://inspect/#remote-debugging switched on there.
export const ATTACH = /^(1|true)$/i.test(process.env.JEVB_ATTACH || "");
export const USER_CHROME = process.env.JEVB_USER_DATA_DIR || join(homedir(), "Library", "Application Support", "Google", "Chrome");
// One daemon per mode, so the dedicated Chrome and your own Chrome never share a socket.
export const SOCKET = join(HOME, ATTACH ? "daemon-attach.sock" : "daemon.sock");

// Jev is served by TypeSafe and by OpenRouter (same request). A key is only ever sent to the
// host of the provider it belongs to.
const PROVIDERS = {
  TYPESAFE_API_KEY: { name: "TypeSafe", url: "https://api.typesafe.ai/v1/systemone", model: "jev-latest" },
  OPENROUTER_API_KEY: { name: "OpenRouter", url: "https://openrouter.ai/api/v1/systemone", model: "~typesafe/jev-latest" },
};

function envFiles() {
  // HOME/.env first; then, in a development checkout of the marketplace repo only, its root .env.
  const out = [join(HOME, ".env")];
  const repo = resolve(PLUGIN_ROOT, "..", "..");
  if (existsSync(join(repo, ".claude-plugin", "marketplace.json"))) out.push(join(repo, ".env"));
  return out;
}

let cached;
export function credentials() {
  if (cached) return cached;
  const keys = {};
  for (const k of Object.keys(PROVIDERS)) if (process.env[k]) keys[k] = process.env[k].trim();
  for (const f of envFiles()) {
    if (!existsSync(f)) continue;
    for (const line of readFileSync(f, "utf8").split("\n")) {
      const m = line.match(/^\s*(?:export\s+)?([A-Z_]+)\s*=\s*["']?([^"'\s]+)["']?/);
      if (m && PROVIDERS[m[1]] && !keys[m[1]]) keys[m[1]] = m[2];
    }
  }
  const k = Object.keys(PROVIDERS).find((n) => keys[n]);
  if (!k) throw new Error("No Jev key: set TYPESAFE_API_KEY (or OPENROUTER_API_KEY) in the environment or in " + join(HOME, ".env"));
  return (cached = { key: keys[k], ...PROVIDERS[k] });
}
