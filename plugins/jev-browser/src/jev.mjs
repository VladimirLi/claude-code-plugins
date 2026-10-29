// Client for Jev (TypeSafe System One). Jev answers typed questions about a `state`; it never
// writes text. Page content only ever goes in `state`; instructions are fixed strings here.
// Images: base64 JPEG/PNG assets, opted in per request with TypeSafe-Danger-Zone-Images
// (https://docs.typesafe.ai/images). The asset's key is a reference string placed in `state`.
import { credentials } from "./paths.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const stats = { calls: 0, ms: 0 };

// questions: { name: { type: "noul" | "choice" | "score", instructions, criteria? } }
// image: base64 JPEG to attach; it is referenced from state.screenshot.
export async function jev(state, questions, { image, timeout = 20_000, retries = 2 } = {}) {
  const { key, url, model, name } = credentials();
  const body = { model, state, questions };
  const headers = { Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
  if (image) {
    const ref = `jevb-shot-${Math.random().toString(36).slice(2, 10)}`;
    body.state = { ...state, screenshot: ref };
    body.assets = { [ref]: { type: "image", format: "jpeg", data_base64: image } };
    headers["TypeSafe-Danger-Zone-Images"] = "true";
  }
  for (let attempt = 0; ; attempt++) {
    const t = performance.now();
    let res, out;
    try {
      res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(timeout) });
      out = await res.json().catch(() => ({}));
    } catch (e) {
      if (attempt < retries) { await sleep(300 * (attempt + 1)); continue; }
      throw new Error(`Jev (${name}) unreachable: ${e.message}`);
    }
    const ms = Math.round(performance.now() - t);
    stats.calls++; stats.ms += ms;
    if (res.ok && out.answers) return { answers: validate(out.answers, questions), ms };
    if (attempt < retries && (res.status === 429 || res.status >= 500)) { await sleep(400 * (attempt + 1)); continue; }
    const msg = JSON.stringify(out).split(key).join("<key>").slice(0, 300);
    throw new Error(`Jev (${name}) ${res.status}: ${msg}`);
  }
}

// Strict checks: a choice must be one of the criteria; probabilities must be numbers.
// An invalid answer becomes null, and callers treat null as "no decision".
function validate(answers, questions) {
  const out = {};
  for (const [k, q] of Object.entries(questions)) {
    const a = answers[k];
    if (!a) { out[k] = null; continue; }
    if (q.type === "noul") out[k] = typeof a.noul === "number" ? a : null;
    else if (q.type === "choice") {
      const ok = a.choice != null && Object.hasOwn(q.criteria, String(a.choice)) && a.probabilities && typeof a.probabilities === "object";
      out[k] = ok ? a : null;
    } else out[k] = a;
  }
  return out;
}
