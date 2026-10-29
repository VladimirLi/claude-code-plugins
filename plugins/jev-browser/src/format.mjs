// Text renderings for Claude (MCP and CLI share them) and the page diff.
import { fence } from "./safety.mjs";

export function brief(e) {
  if (!e) return "?";
  const name = e.label || e.text || e.placeholder || e.name || e.hint || e.near || e.href || "";
  return `${e.tag} "${String(name).slice(0, 50)}"`;
}

export function elementLine(e) {
  const f = [];
  for (const k of ["label", "text", "placeholder", "name"]) if (e[k]) f.push(`${k === "text" ? "" : k + "="}"${e[k]}"`);
  if (e.value !== undefined && e.value !== "") f.push(`value="${e.value}"`);
  if (e.options) f.push(`options=[${e.options.slice(0, 10).join(", ")}${e.options.length > 10 ? ", …" : ""}]`);
  for (const k of ["checked", "disabled", "busy", "expanded", "active", "hidden", "covered"]) if (e[k] !== undefined) f.push(`${k}=${e[k]}`);
  if (e.sorted) f.push(`sorted=${e.sorted}`);
  if (e.row_state) f.push(`row=${e.row_state}`);
  if (e.href && !(e.label || e.text || e.placeholder || e.name)) f.push(`href=${e.href}`);
  if (e.near && !e.text) f.push(`near="${e.near}"`);
  if (e.hint) f.push(`hint="${e.hint}"`);
  if (e.frame) f.push(`frame=${e.frame}`);
  return `${e.new ? "*" : ""}[${e.id}] ${e.tag} ${f.join(" ")}`.trimEnd();
}

// Viewport elements first (DOM order), then off-screen ones, until the cap.
export function renderPage(page, { maxChars = 12_000, filter } = {}) {
  const head = [`url: ${page.url}`, `title: ${page.title}`];
  const body = [];
  if (page.dialogs?.length) body.push(`dialogs/overlays: ${page.dialogs.join(" || ")}`);
  if (!filter) body.push(`visible text: ${page.text}`);
  let els = page.elements;
  if (filter) {
    const n = filter.toLowerCase();
    els = els.filter((e) => ["label", "text", "placeholder", "name", "href", "near", "hint"].some((k) => String(e[k] ?? "").toLowerCase().includes(n)));
  }
  const inView = els.filter((e) => !e.off), off = els.filter((e) => e.off);
  body.push(`elements (${els.length}${filter ? ` matching "${filter}"` : ""}; ${off.length} off-screen; * = new):`);
  let used = head.join("\n").length + body.join("\n").length, shown = 0;
  const ordered = [...inView, ...off];
  for (const e of ordered) {
    const line = (e.off && e === off[0] ? `-- off-screen (scrolled into view when acted on):\n` : "") + elementLine(e);
    if (used + line.length + 1 > maxChars - 200) break;
    body.push(line); used += line.length + 1; shown++;
  }
  if (shown < ordered.length) body.push(`… ${ordered.length - shown} more elements not shown; use observe find <text>`);
  return `${head.join("\n")}\n${fence(body.join("\n"))}`;
}

// Word runs inserted in b relative to a (LCS over words).
export function insertedText(a = "", b = "", max = 300) {
  const A = a.split(" ").slice(0, 500), B = b.split(" ").slice(0, 500), n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const runs = []; let cur = [], i = 0, j = 0;
  while (j < m) {
    if (i < n && A[i] === B[j]) { if (cur.length) { runs.push(cur.join(" ")); cur = []; } i++; j++; }
    else if (i < n && dp[i + 1][j] >= dp[i][j + 1]) i++;
    else { cur.push(B[j]); j++; }
  }
  if (cur.length) runs.push(cur.join(" "));
  return runs.join(" | ").slice(0, max);
}

const STATE_KEYS = ["value", "checked", "expanded", "active", "disabled", "sorted", "covered", "row_state"];
const stateOf = (e) => STATE_KEYS.filter((k) => e[k] !== undefined).map((k) => `${k}=${JSON.stringify(e[k])}`).join(" ");

// Sticky ids make the diff exact: same id = same element.
export function pageDiff(a, b) {
  if (!a) return null;
  const old = new Map(a.elements.map((e) => [e.id, e])), cur = new Map(b.elements.map((e) => [e.id, e]));
  const d = {};
  const added = b.elements.filter((e) => !old.has(e.id)), removed = a.elements.filter((e) => !cur.has(e.id));
  const changed = b.elements.filter((e) => old.has(e.id) && stateOf(old.get(e.id)) !== stateOf(e));
  if (a.url !== b.url) d.url = `${a.url} -> ${b.url}`;
  if (added.length) d.added = added;
  if (removed.length) d.removed = removed.map((e) => `[${e.id}] ${brief(e)}`);
  if (changed.length) d.changed = changed.map((e) => `[${e.id}] ${brief(e)}: ${stateOf(old.get(e.id)) || "(none)"} -> ${stateOf(e) || "(none)"}`);
  const ins = insertedText(a.text, b.text); if (ins) d.new_text = ins;
  if (JSON.stringify(a.dialogs) !== JSON.stringify(b.dialogs)) d.dialogs = b.dialogs;
  return d;
}

// Short version for Jev's state (no element objects).
export function diffForJev(d) {
  if (!d) return undefined;
  const o = { ...d };
  if (o.added) o.added = o.added.slice(0, 15).map((e) => `[${e.id}] ${brief(e)}`);
  if (o.removed) o.removed = o.removed.slice(0, 15);
  if (o.changed) o.changed = o.changed.slice(0, 15);
  return Object.keys(o).length ? o : { nothing_changed: true };
}

export function renderDiff(page, d, { maxChars = 8000 } = {}) {
  const lines = [`url: ${page.url}`, `title: ${page.title}`];
  const body = [];
  if (!d || !Object.keys(d).length) body.push("no change on the page.");
  else {
    if (d.url) body.push(`url changed: ${d.url}`);
    if (d.dialogs) body.push(`dialogs/overlays: ${d.dialogs.join(" || ") || "(none)"}`);
    if (d.added) body.push(`added (${d.added.length}):`, ...d.added.slice(0, 60).map((e) => "  " + elementLine(e)), ...(d.added.length > 60 ? [`  … ${d.added.length - 60} more`] : []));
    if (d.removed) body.push(`removed (${d.removed.length}): ${d.removed.slice(0, 20).join(", ")}${d.removed.length > 20 ? " …" : ""}`);
    if (d.changed) body.push("changed:", ...d.changed.slice(0, 30).map((x) => "  " + x));
    if (d.new_text) body.push(`new text: ${d.new_text}`);
  }
  let text = body.join("\n");
  if (text.length > maxChars) text = text.slice(0, maxChars) + "\n… truncated; use observe full";
  return `${lines.join("\n")}\n${fence(text)}`;
}
