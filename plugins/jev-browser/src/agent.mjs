import { appendFileSync } from "node:fs";
// The five operations (open, do, act, observe, check) plus eval, over one Tab.
// Each returns { text, image?, data } where `text` is what Claude reads.
import { jev, stats } from "./jev.mjs";
import { KEY_NAMES } from "./tab.mjs";
import { brief, elementLine, renderPage, renderDiff, pageDiff, diffForJev } from "./format.mjs";
import { fence, scrub, secretsOf, redactValues, gateRule, dialogRisky, domainAllowed, REDACTED, SECRET_KEY } from "./safety.mjs";
import { haiku, haikuOn, shouldEngage, planFirst, outerLoop, HAIKU_MS, READER, READ_CHARS, PAGE_CHARS, RISKY_LINK } from "./haiku.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MAX_SINGLE = 240, GROUP = 30, MAX_STATE_CHARS = 70_000;
// `wait` rounds are bounded by time, not by count: each one blocks until the page changes
// (or WAIT_STEP ms pass), and all of them together stop after WAIT_BUDGET ms. They do not use
// up max_actions.
const WAIT_STEP = 8000, WAIT_BUDGET = 18_000, MAX_WAITS = 30;
const COMPLETE_Q = { type: "noul", instructions: "Is everything `task.goal` asks for already finished on `page`, so that no further action (such as pressing a submit, search or continue button) is needed?" };
const ACTION_DONE_Q = { type: "noul", instructions: "Has an action in `task.history` already performed what `task.goal` asks for (for example the drag, scroll, click or entry it names), and does `task.last_change` show the page changed the way that action should change it?" };
const MAX_BUDGET = 30, EXTRA_ROUNDS = 5;
const defaultBudget = (values) => Math.min(MAX_BUDGET, Math.max(20, 10 + Object.keys(values ?? {}).length));
// goals that go to a place ("Open the site's pricing page"): the words naming it must be on the page
const NAV_GOAL = /^\s*(open|go to|navigate to|visit|show)\b/i;
// any goal that ends on a page it names ("Scroll down to the X link and open it")
const NAV_ANY = /\b(open|go to|navigate to|visit)\b/i;
const NAV_STOP = new Set("open go to navigate visit show the a an this that its it site site s website web page pages tab tabs link links section area category article view of on in for about and with from linked new main top first second last my our your me repository repo".split(" "));
const IRREVERSIBLE_Q = { type: "noul", instructions: "Would the next action toward `task.goal` on `page` have an effect outside this browser that is hard to undo, such as placing an order, paying, sending a message, deleting data or publishing?" };
const UNSURE = "the page looks done but Jev is unsure; verify with check or observe";
// words of any goal that name no thing on a page (verbs, fillers): the rest must show up in the
// visible text or elements, or the page may not be showing what the goal is about yet
const GOAL_STOP = new Set([...NAV_STOP, ..."close click press type enter fill select choose pick make add remove delete mark check checked uncheck unchecked sort search find look drag drop scroll wait until trigger accept dismiss reveal hover hovering upload submit then given values value field fields button buttons input inputs text there exactly only all each every more load loading start their them they using use what which where when into onto over under name number item items says saying called named labelled labeled".split(" ")]);
// the round-0 wait for content a timer shows (entry ads, delayed modals); it runs alongside
// the first decision, so it costs only what it outlasts that call
const SHOW_WAIT = 800;
const IMAGE_NOTE = " The image `screenshot` shows the visible part of the page: each element of `page.elements` in view is outlined with its `i` in a small red tag at its top-left corner, and its `box` is [x, y, width, height] in the image's pixels.";

const TOOLS = {
  click: "Click the target (link, button, checkbox, radio, tab, menu item, option, table header)",
  type: "Type one of the given `task.values` into the target text field, replacing its content",
  press_enter: "Press Enter in the target field (e.g. to submit a search or add an item)",
  press_key: "Press a keyboard key other than Enter (Escape, Tab, arrow keys, Space, Backspace…)",
  select: "Choose an option in the target <select> dropdown",
  hover: "Move the mouse over the target to reveal hidden content",
  right_click: "Right-click the target to open a context menu",
  drag: "Drag the target and drop it onto another element",
  upload: "Attach one of the given `task.values` (a file path) to the target file input",
  scroll_down: "Scroll down to load or reveal more content",
  scroll_up: "Scroll up to reveal content above",
  wait: "Wait: the page is still loading or processing (spinner, 'loading…', busy/disabled button)",
  none: "No action: the goal is already achieved, or nothing on this page can make progress",
};
const TARGETED = new Set(["click", "type", "press_enter", "select", "hover", "right_click", "drag", "upload"]);
const GUARDED = new Set(["click", "press_enter", "press_key"]);
const FIELDISH = (e) => !!e && (/^(input:(text|email|password|search|tel|url|number|date|datetime-local|month|week|time|color|range)|textarea)/.test(e.tag) || /\[(textbox|searchbox|combobox|spinbutton)\]/.test(e.tag) || e.tag.startsWith("input:") && !/checkbox|radio|submit|button|reset|file|image/.test(e.tag));
const SELECTISH = (e) => !!e && (e.tag === "select" || !!e.options);
const FILEISH = (e) => !!e && e.tag === "input:file";
// a goal that asks for a search, and a field that runs one: typing into it is not the search
const SEARCHY_GOAL = /\b(search|find|look\s*up|filter|query)\b/i;
const SEARCH_FIELD = (e) => !!e && (e.tag === "input:search" || /\[(searchbox|combobox)\]/.test(e.tag) || /search|filter|find|query/i.test(`${e.label ?? ""} ${e.placeholder ?? ""} ${e.name ?? ""}`));

// Dropdown options against the words of the goal (or of the given value). An option is named
// when its whole text appears in the words; "Option 7" names a missing sibling of "Option 2"
// when an option's shape (its text with numbers as wildcards) matches the words but no option does.
const norm = (s) => String(s ?? "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const PLACEHOLDER_OPT = /^(please )?(select|choose|pick)\b|^-+$|^$/i;
const NO_OPTION = "(none of these options)";
function namedOption(opts, words, fromValue) {
  const w = ` ${norm(words)} `, nw = norm(words);
  return opts.filter((o) => !PLACEHOLDER_OPT.test(o.trim()) && norm(o) && (w.includes(` ${norm(o)} `) || (fromValue && nw && norm(o).includes(nw))))
    .sort((a, b) => b.length - a.length)[0];
}
const stem = (w) => w.slice(0, Math.min(w.length, Math.max(5, w.length - 2)));

// A NAV_GOAL met by its last click: a link click (no error) that moved to a new page whose
// title, url or top text carries the link's own name. The page alone rarely proves "the second
// book listed there" (books: confirm 0.03 on the right book page), but the click Jev chose for
// the goal landed where its link said. Returns the link name, or null.
export function arrivedAt(page, history) {
  const last = history.filter((h) => h.action && h.action !== "wait").at(-1);
  if (!last || last.error || last.action !== "click" || !last.name || !/^a$|\[link\]$/.test(last.tag ?? "")) return null;
  if (!last.url || last.url.split("#")[0] === page.url.split("#")[0]) return null;
  // listings cut long names ("Full Moon over Noah's Ark: An ..."): drop a cut last word
  const words = norm(String(last.name).replace(/(\S*)(\.\.\.|…)\s*$/, "")).split(" ").filter(Boolean).slice(0, 6);
  if (!words.length) return null;
  const phrase = ` ${words.join(" ")} `, hay = ` ${norm(`${page.title} ${decodeURIComponent(page.url).replace(/[-_/]/g, " ")} ${page.text.slice(0, 400)}`)} `;
  return hay.includes(phrase) ? last.name : null;
}
// "Scroll down to the customer reviews section": the words naming the place, or null for
// other goals, goals that go on after the scroll ("... and open it"), and scrolls that load
// content ("Scroll down to load more paragraphs": infinite scroll, no place to reach)
const SCROLL_GOAL = /^\s*scroll\s+(?:(?:down|up)\s+)?(?:to|until)\s+(.+)$/i;
export function scrollNames(goal) {
  const m = String(goal).match(SCROLL_GOAL);
  if (!m || /\b(and|then)\b|,/.test(m[1]) || /^(load|show|reveal|get|fetch|trigger|display)\b|\b(load|loads|loaded|loading|more|new|next)\b/i.test(m[1])) return null;
  const words = norm(m[1]).split(" ").filter((w) => w.length >= 3 && !GOAL_STOP.has(w) && !/^(bottom|end|top|part|heading|you|see|reach|reached|visible|shown|appears)$/.test(w));
  return words.length ? words : null;
}
// the goal names this element: a content word of its name is in the goal (infinite-scroll's
// footer link "Elemental Selenium" is a scroll target the goal never names)
const goalNames = (el, goal) => { const g = ` ${norm(goal)} `; return norm(el?.text || el?.label || "").split(" ").some((w) => w.length >= 3 && !GOAL_STOP.has(w) && g.includes(` ${stem(w)}`)); };
// every word in the text the viewport shows
const inView = (page, words) => { const hay = ` ${norm(page.text)} `; return words.every((w) => hay.includes(` ${stem(w)}`)); };

function missingSibling(opts, words) {
  const w = norm(words), have = new Set(opts.map(norm));
  for (const o of opts) {
    const n = norm(o);
    if (!/\d/.test(n) || !/\p{L}/u.test(n)) continue;
    const re = new RegExp(`(?:^| )(${n.split(/(\d+)/).map((p, k) => (k % 2 ? "\\d+" : p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))).join("")})(?= |$)`);
    const m = w.match(re);
    if (m && !have.has(m[1])) return m[1];
  }
  return null;
}

// Jev sees elements keyed by `i`; Claude sees `[id]`. Same number. `typed` maps element id ->
// the values key typed into it, so a filled password field shows which value it holds (never the value).
function repeated(elements) {
  const counts = {};
  for (const e of elements) { const k = brief(e); counts[k] = (counts[k] ?? 0) + 1; }
  const rep = Object.entries(counts).filter(([, n]) => n > 1).sort((a, b) => b[1] - a[1]).slice(0, 10);
  return rep.length ? Object.fromEntries(rep) : undefined;
}
const forJev = (page, typed, boxes) => ({
  url: page.url, title: page.title, text: page.text, dialogs: page.dialogs?.length ? page.dialogs : undefined, tables: page.tables, metrics: page.metrics,
  repeated_elements: repeated(page.elements),
  elements: page.elements.map(({ id, new: _n, submitter: _s, submits: _b, collects: _c, ...e }) => ({ i: id, ...e, ...(e.value === "[filled]" && typed?.has(id) ? { value: `[filled with values.${typed.get(id)}]` } : {}), ...(boxes?.[id] ? { box: boxes[id] } : {}) })),
});

// forJev within the single-request budget: big pages (Wikipedia, HN) keep the first elements
// that fit, so the confirm questions never exceed Jev's input limit.
function forJevCapped(page, typed) {
  const jp = forJev(page, typed);
  for (let n = Math.min(jp.elements.length, MAX_SINGLE); n > 0; n = Math.floor(n / 2)) {
    const t = n < jp.elements.length ? { ...jp, elements: jp.elements.slice(0, n) } : jp;
    if (JSON.stringify(t).length <= MAX_STATE_CHARS) return t;
  }
  return { ...jp, elements: [], text: jp.text.slice(0, 20_000) };
}

// history as Jev sees it: without the element id and url code keeps for its own checks
const hist = (hs) => hs.map(({ id: _i, url: _u, tag: _t, name: _n, ...h }) => h);

// The diff Jev sees, plus the JS dialog the last action opened: the DOM shows no trace of an
// alert the tab accepted, so without this the judges read "nothing changed" and the loop
// repeats the action (context-menu: right_click fired the alert twice, then ended as stuck).
function withDialog(change, last) {
  if (!last?.dialog) return change;
  const { nothing_changed: _n, ...rest } = change ?? {};
  return { ...rest, dialog: last.dialog };
}

// The exact call that runs a gated action once the user wants it (saves the driver composing it).
function resumeLine(p) {
  if (!p?.op) return null;
  const cli = [p.op, p.id != null ? p.id : null, p.key ?? null].filter((x) => x != null).join(" ");
  const json = JSON.stringify({ op: p.op, ...(p.id != null ? { id: p.id } : {}), ...(p.key ? { key: p.key } : {}) });
  return `resume: jevb act ${cli} --allow-irreversible  (MCP: act [${json}] allow_irreversible=true)`;
}
// what Enter or Space without a target would press: the focused element, as a gate brief
const needsFocused = (op, id, key) => op === "press_key" && id == null && /^(Enter|NumpadEnter|Space| )$/.test(String(key ?? ""));
// a values key to suggest for a field: "query" for a search box, else its label in snake case
const valueKeyFor = (e) => (SEARCH_FIELD(e) ? "query" : norm(e?.label || e?.placeholder || e?.name || "text").split(" ").slice(0, 3).join("_") || "text");
// an act step that submits a form: a click on its submit button, or Enter in one of its fields
const submitsForm = (op, el, key) => !!el && (["click", "dblclick"].includes(op) ? !!el.submits
  : (op === "press_enter" || (op === "press_key" && /^(Enter|NumpadEnter)$/.test(String(key ?? "")))) && !!(el.submits || el.submitter || el.collects));
// the info line for a submit the in-page guard stopped
const heldInfo = (h) => (h.collects ? `the action submitted a form that collects contact or payment details (${h.collects}) by "${h.submit}"; the submit was stopped` : `the action submitted a form whose button "${h.submit}" commits something outside the browser; the submit was stopped`);

function repeatsBlock(seq, k, times) {
  if (seq.length < k * times) return false;
  const tail = seq.slice(-k * times), block = tail.slice(0, k).join("\n");
  if (k > 1 && new Set(tail.slice(0, k)).size === 1) return false;
  for (let n = 1; n < times; n++) if (tail.slice(n * k, (n + 1) * k).join("\n") !== block) return false;
  return true;
}

export class Agent {
  constructor(tab) { this.tab = tab; this.shown = null; this.secrets = []; this.typed = new Map(); }

  remember(values) { for (const s of secretsOf(values)) if (!this.secrets.includes(s)) this.secrets.push(s); }
  out(text, data = {}) { return { text: scrub(text, this.secrets), data: scrub(data, this.secrets) }; }
  events() { return this.tab.events.splice(0); }

  // A short summary by default (url, title, element count, the start of the visible text):
  // the element table and full text cost up to ~12k chars; `full` (or observe full) prints them.
  async open({ url, full = false }) {
    if (!/^[a-z]+:/i.test(url)) url = `https://${url}`;
    if (!domainAllowed(url)) return this.out(`status: blocked\ninfo: ${url} is outside JEVB_ALLOWED_DOMAINS`, { status: "blocked" });
    const t0 = Date.now();
    // with the Haiku loop on, its child starts now, so a first Haiku turn does not wait for it
    if (haikuOn()) { const c = haiku(); c.begin(); c.end(); }
    await this.tab.navigate(url);
    const page = await this.tab.snapshot();
    this.shown = page;
    const ev = this.events();
    const inView = page.elements.filter((e) => !e.off && !e.hidden).length;
    const view = full ? renderPage(page) : `url: ${page.url}\ntitle: ${page.title}\nelements: ${page.elements.length} (${inView} in view; observe full lists them with their ids)\n${fence(`visible text: ${page.text.slice(0, 400)}${page.text.length > 400 ? " …" : ""}${page.dialogs?.length ? `\ndialogs/overlays: ${page.dialogs.join(" || ").slice(0, 300)}` : ""}`)}`;
    return this.out(`opened in ${Date.now() - t0} ms${ev.length ? `\nevents: ${ev.join("; ")}` : ""}${page.captcha ? "\nwarning: the page shows a captcha or bot check (needs_human)" : ""}\n${view}`, { url: page.url, title: page.title, elements: page.elements.length, ms: Date.now() - t0 });
  }

  async observe({ mode = "full", query } = {}) {
    await this.tab.settle({ cap: 1500 });
    if (mode === "screenshot") {
      const image = await this.tab.screenshot();
      return { ...this.out(`screenshot of ${await this.tab.url()}`), image };
    }
    if (mode === "read") {
      const t = await this.tab.J("readText", 15_000);
      return this.out(`url: ${await this.tab.url()}\n${query ? `question: ${query}\n` : ""}${fence(t)}`);
    }
    const page = await this.tab.snapshot();
    if (mode === "diff" && this.shown) {
      const d = pageDiff(this.shown, page); this.shown = page;
      return this.out(renderDiff(page, d));
    }
    this.shown = page;
    return this.out(renderPage(page, mode === "find" ? { filter: query || "" } : {}));
  }

  async check({ question, vision = false }) {
    await this.tab.settle({ cap: 1500 });
    const page = await this.tab.snapshot();
    const image = vision ? await this.shot(page) : undefined;
    const st = scrub({ page: forJevCapped(page) }, this.secrets);
    const { answers, ms } = await jev(st, { q: { type: "noul", instructions: `Answer about \`page\`${vision ? " and the image `screenshot`" : ""}: ${question}` } }, { image });
    const p = answers.q?.noul;
    return this.out(`p_yes: ${p == null ? "unknown" : p.toFixed(2)} (${ms} ms)`, { p_yes: p, ms });
  }

  async evalJs({ js }) {
    const src = String(js).trim();
    const expr = /^(async\s*)?(\(|function\b)/.test(src) ? `(async () => (${src})())()` : src;
    const v = await this.tab.eval(expr);
    return this.out(JSON.stringify(v ?? null), { value: v });
  }

  // Claude-chosen actions, executed in order. Ids are checked before anything runs. Stops at
  // the first error, a URL change, or a gated action.
  async act({ actions, values = {}, allow_irreversible = false }) {
    this.remember(values);
    if (!Array.isArray(actions) || !actions.length) throw new Error("actions must be a non-empty list");
    const before = this.shown ?? await this.tab.snapshot();
    const byId = new Map(before.elements.map((e) => [e.id, e]));
    const ids = actions.flatMap((a) => [a.id, a.to]).filter((x) => x != null).map(Number);
    const known = await this.tab.eval(`(() => { const J = window.__jevb; return ${JSON.stringify(ids)}.filter(i => !J || !J.els.has(i) || !J.els.get(i).el.isConnected) })()`).catch(() => ids);
    if (known.length) throw new Error(`unknown or stale element id(s) ${known.join(", ")}; observe again`);
    const results = []; let status = "ok", pending, info, asked = false;
    const url0 = await this.tab.url();
    if (allow_irreversible) await this.tab.allowSubmits(true);
    try {
    for (const [n, a] of actions.entries()) {
      const op = a.op;
      const value = a.value_key != null ? values[a.value_key] : a.value;
      if (a.value_key != null && value === undefined) { status = "error"; results.push({ op, id: a.id, error: `no value with key "${a.value_key}"` }); break; }
      const el = byId.get(Number(a.id));
      const key = a.key || a.value;
      const focused = !allow_irreversible && needsFocused(op, a.id, key) ? await this.tab.J("focused").catch(() => null) : null;
      const rule = !allow_irreversible && gateRule(op, el, before, { key, focused });
      if (rule) { status = "needs_confirmation"; pending = { op, id: a.id, element: brief(el ?? focused), ...(op === "press_key" ? { key } : {}) }; info = `${rule}; repeat with allow_irreversible to do it`; break; }
      // a step that submits a form: Jev's irreversible question, once per call; it can add a
      // pause, never remove one (the code rule above already ran)
      if (!allow_irreversible && !asked && submitsForm(op, el ?? focused, key)) {
        asked = true;
        const p = await this.irreversible(before, `${op === "dblclick" ? "click" : op} ${brief(el ?? focused)}`).catch(() => 0);
        if (p >= 0.6) { status = "needs_confirmation"; pending = { op, id: a.id, element: brief(el ?? focused), ...(op === "press_key" ? { key } : {}) }; info = `Jev rates this form submit as hard to undo (${p.toFixed(2)}); repeat with allow_irreversible to do it`; break; }
      }
      this.tab.dialogPolicy = (d) => (allow_irreversible || !dialogRisky(d.message) ? { accept: true, text: values.prompt } : { accept: false, held: true });
      this.tab.held = null;
      const since = Date.now();
      try {
        const note = await this.tab.exec({ op, id: a.id != null ? Number(a.id) : undefined, value, key: a.key, to: a.to != null ? Number(a.to) : undefined, direction: a.direction });
        await this.tab.settle({ since });
        if (!allow_irreversible) await this.tab.takeHeld();
        results.push({ op, id: a.id, ok: true, ...(note ? { note } : {}) });
      } catch (e) {
        results.push({ op, id: a.id, error: e.message.slice(0, 200) }); status = "error"; break;
      }
      if (this.tab.held) {
        status = "needs_confirmation";
        info = this.tab.held.submit ? `${heldInfo(this.tab.held)}; repeat with allow_irreversible` : `a "${this.tab.held.message.slice(0, 80)}" dialog looked irreversible and was dismissed; repeat with allow_irreversible`;
        pending = { op, id: a.id, element: brief(el), ...(op === "press_key" ? { key } : {}) };
        break;
      }
      const u = await this.tab.url();
      if (!domainAllowed(u)) { await this.tab.exec({ op: "back" }).catch(() => {}); status = "blocked"; info = `navigation to ${u} is outside JEVB_ALLOWED_DOMAINS`; break; }
      if (u !== url0 && n < actions.length - 1) { info = `the page navigated after action ${n + 1}; the remaining ${actions.length - n - 1} were not run`; break; }
    }
    } finally { if (allow_irreversible) await this.tab.allowSubmits(false); }
    const page = await this.tab.snapshot();
    const ev = this.events();
    const navigated = page.url.split("#")[0] !== before.url.split("#")[0];
    const view = navigated ? renderPage(page) : renderDiff(page, pageDiff(before, page));
    this.shown = page;
    const lines = [`status: ${status}`, ...results.map((r) => `- ${r.op}${r.id != null ? ` [${r.id}]` : ""}: ${r.error ? `ERROR ${r.error}` : `ok${r.note ? ` (${r.note})` : ""}`}`)];
    if (info) lines.push(`info: ${info}`);
    if (pending) lines.push(`pending: ${pending.op}${pending.id != null ? ` [${pending.id}]` : ""}${pending.key ? ` key=${pending.key}` : ""} ${pending.element}`, resumeLine(pending));
    if (ev.length) lines.push(`events: ${ev.join("; ")}`);
    return this.out(`${lines.join("\n")}\n${view}`, { status, results, pending });
  }

  // ---------------------------------------------------------------- Jev loop
  async decide(page, goal, values, history, lastChange, image) {
    const shownValues = redactValues(values);
    const hasValues = Object.keys(values).length > 0;
    const task = { goal, ...(hasValues ? { values: shownValues } : {}), history, ...(lastChange ? { last_change: lastChange } : {}) };
    const withValues = hasValues ? ", with the given `task.values`" : "";
    const tools = { ...TOOLS };
    // with no values `type` stays a choice, so a goal that needs text stops as needs_value
    // instead of pressing keys ("Search for Alan Turing": Space, Backspace, … until stuck)
    if (!hasValues) { tools.type = "Type text into the target text field (the text is not given in `task`)"; delete tools.upload; }
    if (!page.elements.some(FILEISH)) delete tools.upload;
    if (!page.elements.some(SELECTISH)) delete tools.select;
    const common = {
      done: { type: "noul", instructions: `Does \`page\` show that \`task.goal\` has been achieved${withValues}? Judge from \`page.text\` and \`page.elements\`.` },
      done_change: { type: "noul", instructions: `Does \`page\` show that \`task.goal\` has been achieved${withValues}? Judge from \`page.text\`, \`page.elements\` and \`task.last_change\` (what the last action changed).` },
      blocked: { type: "noul", instructions: "Is there something on `page` that stops progress on `task.goal` and cannot be handled by clicking or typing (captcha, access denied, error page)?" },
      error: { type: "noul", instructions: "Does `page` show an error or rejection message (e.g. invalid credentials, a validation error, not found) caused by the actions in `task.history`?" },
      login: { type: "noul", instructions: "Is `page` a sign-in or sign-up screen, or asking the user to log in, before `task.goal` can continue?" },
      irreversible: IRREVERSIBLE_Q,
      tool: { type: "choice", instructions: "What is the next action toward `task.goal` on `page`, given what `task.history` already did?", criteria: tools },
    };
    if (hasValues) common.value = { type: "choice", instructions: "If the next action toward `task.goal` types, selects or uploads something, which of `task.values` should it use? Prefer values not yet entered on `page`.", criteria: Object.fromEntries(Object.entries(shownValues).map(([k, v]) => [k, v])) };
    const targetQ = { type: "choice", instructions: "Which entry of `page.elements` (by its `i`) should the next action toward `task.goal` act on?" };
    const k = this.tab.shotScale ?? 1;   // boxes in the image's pixels (a shrunk capture shrinks them too)
    const jp = forJev(page, this.typed, image ? (k === 1 ? page.boxes : Object.fromEntries(Object.entries(page.boxes ?? {}).map(([id, b]) => [id, b.map((n) => Math.round(n * k))]))) : null);
    if (image) targetQ.instructions += IMAGE_NOTE;
    const call = (state, qs) => jev(scrub(state, this.secrets), qs, { image });
    if (page.elements.length <= MAX_SINGLE && JSON.stringify(jp).length <= MAX_STATE_CHARS) {
      const qs = { ...common };
      if (page.elements.length) qs.target = { ...targetQ, criteria: Object.fromEntries(page.elements.map((e) => [String(e.id), null])) };
      const r = await call({ page: jp, task }, qs);
      return { ...r.answers, stages: 1 };
    }
    // Two stages: groups of consecutive elements, then elements in the likeliest groups.
    const groups = [];
    for (let k = 0; k < jp.elements.length; k += GROUP) {
      const els = jp.elements.slice(k, k + GROUP);
      groups.push({ g: groups.length, summary: els.map((e) => (e.label || e.text || e.placeholder || e.href || e.tag).slice(0, 24)).join(" | ").slice(0, 300) });
    }
    const lite = { url: jp.url, title: jp.title, text: jp.text, metrics: jp.metrics, dialogs: jp.dialogs, groups };
    const r1 = await call({ page: lite, task }, {
      ...common,
      group: { type: "choice", instructions: "Which entry of `page.groups` (by its `g`) contains the element the next action toward `task.goal` should act on? Each group summarises consecutive page elements.", criteria: Object.fromEntries(groups.map((g) => [String(g.g), null])) },
    });
    if (!r1.answers.group) return { ...r1.answers, stages: 2 };
    const ranked = Object.entries(r1.answers.group.probabilities).sort((a, b) => b[1] - a[1]);
    const pick = []; let mass = 0;
    for (const [g, p] of ranked) { if (pick.length && (mass >= 0.9 || pick.length >= 4)) break; pick.push(+g); mass += p; }
    const sub = jp.elements.filter((_, k) => pick.includes(Math.floor(k / GROUP))).slice(0, MAX_SINGLE);
    const r2 = await call({ page: { url: jp.url, title: jp.title, text: jp.text.slice(0, 1200), elements: sub }, task }, {
      target: { ...targetQ, criteria: Object.fromEntries(sub.map((e) => [String(e.i), null])) },
    });
    return { ...r1.answers, target: r2.answers.target, stages: 2 };
  }

  resolve(page, a, values) {
    let tool = a.tool?.choice ?? "none";
    const byI = new Map(page.elements.map((e) => [String(e.id), e]));
    const ranked = a.target ? Object.entries(a.target.probabilities).sort((x, y) => y[1] - x[1]) : [];
    const fits = { type: FIELDISH, press_enter: (e) => FIELDISH(e) || e?.tag.startsWith("input"), select: SELECTISH, upload: FILEISH };
    let [ti, tp] = ranked[0] ?? [null, 0];
    // Enter on a button or link means pressing it
    if (tool === "press_enter" && ti != null && /^(button|a|input:(submit|button|image))$|\[(button|link)\]$/.test(byI.get(ti)?.tag ?? "")) tool = "click";
    if (fits[tool] && ti != null && !fits[tool](byI.get(ti))) {
      const alt = ranked.find(([i]) => fits[tool](byI.get(i)));
      if (alt && (alt[1] >= 0.1 || tool === "upload")) [ti, tp] = alt;
      else if (tool === "upload") { const f = page.elements.find(FILEISH); if (f) [ti, tp] = [String(f.id), 0.5]; }
      else if (tool === "type" || tool === "select") tool = "click";
    }
    let valueKey = a.value?.choice;
    if (valueKey == null && Object.keys(values).length === 1) valueKey = Object.keys(values)[0];
    return {
      tool, p_tool: a.tool?.probabilities?.[tool] ?? 0, target: ti == null ? null : +ti, p_target: tp ?? 0, el: byI.get(ti),
      valueKey, value: valueKey != null ? values[valueKey] : undefined,
      candidates: ranked.slice(0, 3).map(([i, p]) => ({ id: +i, p: +p.toFixed(2), el: brief(byI.get(i)) })),
    };
  }

  // The confirm question behind every ending. The state carries no action history on purpose:
  // a history of repeated clicks ("Add Element" x2) biased Jev toward yes (0.52 at 2 of 3 buttons
  // with history, 0.03 without); the page alone must show the goal is met.
  async complete(page, goal, values, dialogs) {
    const c = await jev(scrub({ page: { ...forJevCapped(page, this.typed), ...(dialogs?.length ? { js_dialogs_shown: dialogs } : {}) }, task: { goal, ...(Object.keys(values).length ? { values: redactValues(values) } : {}) } }, this.secrets), { complete: COMPLETE_Q });
    return +(c.answers.complete?.noul ?? 0).toFixed(2);
  }

  // The goal's naming words (for a NAV_GOAL) that appear nowhere in the whole page text, title,
  // url or element labels and links, or null when any of them does (or none can be judged).
  async missingNames(page, goal) {
    const words = norm(goal.replace(NAV_GOAL, "")).split(" ").filter((w) => w.length >= 3 && !NAV_STOP.has(w) && !/^\d+$/.test(w));
    if (!words.length) return null;
    const full = await this.tab.J("readText", 200_000).catch(() => page.text);
    const hay = ` ${norm([page.url, page.title, full, ...page.elements.flatMap((e) => [e.label, e.text, e.href, e.name, e.placeholder])].filter(Boolean).join(" "))} `;
    return words.every((w) => !hay.includes(` ${stem(w)}`)) ? words : null;
  }

  // Content words of any goal that appear nowhere in the visible page (text, title, url,
  // element labels), or null when every one does. A word missing here means the page may not
  // show yet what the goal is about (an ad a timer opens), or shows it only as pixels.
  async unseen(page, goal) {
    const words = norm(goal.replace(/'[^']*'|"[^"]*"/g, " ")).split(" ").filter((w) => w.length >= 4 && !GOAL_STOP.has(w) && !/\d/.test(w));
    if (!words.length) return null;
    const hayOf = (t) => ` ${norm([page.url, page.title, t, ...(page.dialogs ?? []), ...page.elements.flatMap((e) => [e.label, e.text, e.href, e.name, e.placeholder, e.hint, e.near])].filter(Boolean).join(" "))} `;
    let hay = hayOf(page.text), miss = words.filter((w) => !hay.includes(` ${stem(w)}`));
    if (miss.length && page.metrics?.text_length > page.text.length) {
      hay = hayOf(await this.tab.J("readText", 200_000).catch(() => page.text));
      miss = words.filter((w) => !hay.includes(` ${stem(w)}`));
    }
    return miss.length ? miss : null;
  }

  // Jev's irreversible question (the one `do` asks each round) for one act step, named as the goal
  async irreversible(page, step) {
    const x = await jev(scrub({ page: forJevCapped(page, this.typed), task: { goal: step, history: [] } }, this.secrets), { irreversible: IRREVERSIBLE_Q });
    return +(x.answers.irreversible?.noul ?? 0).toFixed(2);
  }

  // A screenshot with each in-view element's [i] mark drawn on it.
  // None while a secret value typed into a plain (non-password) field is in view: Jev gets
  // secrets by key only, and pixels would show the value.
  shot(page) {
    const exposed = page.elements.some((e) => page.boxes?.[e.id] && e.tag !== "input:password" && SECRET_KEY.test(this.typed.get(e.id) ?? ""));
    return exposed ? Promise.resolve(undefined) : this.tab.screenshot({ boxes: page.boxes }).catch(() => undefined);
  }

  // Whether the action a goal names (drag, scroll…) was done, judged from history and the last diff.
  async actionDone(page, goal, history, lastChange) {
    const x = await jev(scrub({ page: forJevCapped(page, this.typed), task: { goal, history: hist(history.filter((h) => h.action || h.event).slice(-10)), ...(lastChange ? { last_change: lastChange } : {}) } }, this.secrets), { action_done: ACTION_DONE_Q });
    return +(x.answers.action_done?.noul ?? 0).toFixed(2);
  }

  // allow_irreversible also opens the page's submit guard (SUBMIT_GUARD) for this call only.
  // The Haiku outer loop (opt-in) never runs with allow_irreversible: the caller's consent covers
  // the caller's own goal, not sub-goals Haiku writes.
  async do(args) {
    if (!args.allow_irreversible) return haikuOn(args) ? this.withHaiku(args) : this.loop(args);
    await this.tab.allowSubmits(true);
    try { return await this.loop(args); } finally { await this.tab.allowSubmits(false); }
  }

  // The Haiku outer loop. Most goals: a plain do first (the child warms up meanwhile), and Haiku
  // only when it fell short (shouldEngage). Goals that need comparing or a fact the goal only
  // implies (planFirst: "the cheapest book", "every product under $20") go to Haiku first, since
  // a plain do reports a wrong pick as done. One wall-clock budget covers everything.
  async withHaiku(args) {
    const t0 = Date.now(), deadline = t0 + HAIKU_MS, child = haiku();
    child.begin();
    try {
      const early = planFirst(args.goal);
      const first = early ? null : await this.loop({ ...args, deadline });
      if (first && !shouldEngage(args.goal, first.data, first.text)) return first;
      const r = await outerLoop({
        goal: args.goal, values: args.values ?? {}, first, t0,
        run: (goal, values, dl) => this.loop({ goal, values, vision: args.vision, deadline: dl }),
        ask: async (m, ms) => {
          const msg = scrub(m, this.secrets), reply = await child.ask(msg, ms);
          // development only: JEVB_HAIKU_TRACE names a file that gets each message and reply
          if (process.env.JEVB_HAIKU_TRACE) try { appendFileSync(process.env.JEVB_HAIKU_TRACE, `\n===== message\n${msg}\n===== reply\n${scrub(reply, this.secrets)}\n`, { mode: 0o600 }); } catch {}
          return reply;
        },
        page: async () => this.haikuPage(),
        go: (href) => this.haikuGo(href),
        read: (urls) => this.haikuRead(urls),
        click: (id) => this.haikuClick(id),
      });
      // Haiku could not start or stopped before any sub-goal: the plain do, as without it
      if (r.fallback) {
        const plain = await this.loop({ ...args, deadline });
        return this.out(`${plain.text}\nhaiku: ${r.data.haiku.why ?? r.data.haiku.outcome}; this is the plain result`, { ...plain.data, ...r.data });
      }
      return this.out(r.text, r.data);
    } finally { child.end(); }
  }

  // The page as Haiku reads it (fenced by the caller): url, title, the form controls, then the
  // text in reading order with links as [text](href).
  async haikuPage() {
    const p = await this.tab.snapshot().catch(() => this.shown);
    if (!p) return "";
    this.shown = p;
    const [text] = await this.tab.eval(`(${READER})(null, ${PAGE_CHARS})`, { timeout: 5000 }).catch(() => [p.text.slice(0, PAGE_CHARS)]);
    // form fields with their ids (a search box sits outside <main>); buttons show inline in the text
    const fields = p.elements.filter((e) => !e.hidden && (/^(input|textarea|select)/.test(e.tag) || /\[(combobox|textbox|searchbox)\]/.test(e.tag)));
    const controls = fields.sort((a, b) => !!a.off - !!b.off).slice(0, 25);
    return [`url: ${p.url}`, `title: ${p.title}`, ...(p.dialogs?.length ? [`dialogs/overlays: ${p.dialogs.join(" || ").slice(0, 300)}`] : []),
      `form fields (${controls.length} of ${fields.length}):`, ...controls.map(elementLine),
      `page text, links as [text](href):`, text].join("\n");
  }

  // Haiku's `go`: open a link a page view showed, as a GET navigation (never a form submit).
  async haikuGo(href) {
    const t0 = Date.now();
    let url;
    try { url = new URL(href, this.shown?.url ?? (await this.tab.url())).href; } catch { return this.out(`status: error\ninfo: not a link: ${href}`, { status: "error" }); }
    if (!/^https?:/.test(url) || RISKY_LINK.test(url)) return this.out(`status: blocked\ninfo: ${url} is not opened by the Haiku loop (not http, or a link that may change state)`, { status: "blocked" });
    if (!domainAllowed(url)) return this.out(`status: blocked\ninfo: ${url} is outside JEVB_ALLOWED_DOMAINS`, { status: "blocked" });
    try { await this.tab.navigate(url); } catch (e) { return this.out(`status: error\ninfo: ${String(e.message).slice(0, 120)}`, { status: "error" }); }
    const page = await this.tab.snapshot();
    this.shown = page;
    this.events();
    if (page.captcha) return this.out(`status: needs_human\ninfo: the page shows a captcha or bot check; a person has to solve it\nurl: ${page.url}\ntitle: ${page.title}`, { status: "needs_human" });
    return this.out(`status: done\ninfo: opened the link ${href}\nactions (1, 0 Jev calls, ${Date.now() - t0} ms):\n  - go ${href}\nurl: ${page.url}\ntitle: ${page.title}\n${fence(`visible text: ${page.text.slice(0, 700)}`)}`, { status: "done", url: page.url, ms: Date.now() - t0 });
  }

  // Haiku's `click`: one act click, with act's irreversible gates (never allow_irreversible).
  // act's ok is the click's done: it ran and the page settled.
  async haikuClick(id) {
    let r;
    try { r = await this.act({ actions: [{ op: "click", id }] }); }
    catch (e) { return this.out(`status: error\ninfo: ${String(e.message).slice(0, 120)}`, { status: "error" }); }
    return r.data.status === "ok" ? { text: r.text.replace(/^status: ok/, "status: done"), data: { ...r.data, status: "done" } } : r;
  }

  // Haiku's `read`: pages of the current site, fetched in the page without leaving it.
  async haikuRead(urls) {
    const ok = urls.filter((u) => !RISKY_LINK.test(u) && domainAllowed((() => { try { return new URL(u, this.shown?.url).href; } catch { return ""; } })()));
    const out = ok.length ? await this.tab.eval(`(${READER})(${JSON.stringify(ok)}, ${READ_CHARS})`, { timeout: 15_000 }) : [];
    return [...out, ...urls.filter((u) => !ok.includes(u)).map((u) => `page ${u}: not read (a link that may change state, or outside JEVB_ALLOWED_DOMAINS)`)];
  }

  async loop({ goal, values = {}, max_actions, allow_irreversible = false, vision = false, deadline }) {
    const t0 = Date.now(), calls0 = stats.calls, tab = this.tab;
    // The budget is a backstop, not the stop rule: the repeat checks, the wait time limit and
    // the fallback cap end bad runs. A fixed 10 cut saucedemo-coarse (11 actions) one click short.
    max_actions = Math.min(Math.max(1, max_actions | 0 || defaultBudget(values)), MAX_BUDGET);
    let limit = max_actions;
    this.remember(values);
    const history = [], rounds = [];
    let scrollWords = null, shownAtStart = false, prev = null, page = null, waits = 0, waitedMs = 0, status = "max_actions", info, pending, retried = false, gate, conf = null, fallbacks = 0, lastChange, showWaited = false;
    const seen = new Map();
    // dialogs opened by this do's actions: part of what the page showed, for the confirm question
    const dialogs = () => history.filter((h) => h.dialog).map((h) => h.dialog);
    tab.dialogPolicy = (d) => (allow_irreversible || !dialogRisky(d.message) ? { accept: true, text: values.prompt } : { accept: false, held: true });
    for (let round = 0; round <= limit; round++) {
      page = await tab.snapshot();
      for (const e of this.events()) history.push({ event: e });
      if (page.captcha) { status = "needs_human"; info = "the page shows a captcha or bot check; a person has to solve it"; break; }
      // a scroll goal whose named place was out of view at the start and is in view now: done
      // (the page-only confirm cannot tell "scrolled to the reviews" from "the page has reviews")
      if (round === 0) scrollWords = scrollNames(goal), shownAtStart = scrollWords && inView(page, scrollWords);
      if (round > 0 && scrollWords && !shownAtStart && history.some((h) => /^scroll/.test(h.action ?? "") && !h.error) && inView(page, scrollWords)) {
        status = "done"; info = `scrolled until ${scrollWords.map((w) => `"${w}"`).join(", ")} came into view`; break;
      }
      // The image goes in only on signals from the loop: --vision, a page with no elements, the
      // last action hit a covering element, or (after a text decision) a weak target or a goal
      // word the text does not show. Each screenshot costs a capture and a heavier Jev call.
      const acted = history.some((h) => h.action && h.action !== "wait");
      const lastErr = history.filter((h) => h.action && h.action !== "wait").at(-1)?.error ?? "";
      let why = vision ? "vision" : page.elements.length === 0 ? "no_elements" : /covered by/.test(lastErr) ? "covered" : null;
      let image = why ? await this.shot(page) : undefined;
      const missing = await this.unseen(page, goal).catch(() => null);
      lastChange = round ? withDialog(diffForJev(pageDiff(prev, page)), history.filter((h) => h.action && h.action !== "wait").at(-1)) : undefined;
      // round 0 and the goal names something the page does not show: a timer may be about to
      // show it (entry_ad: the modal opens ~1 s after load, with the tab idle), so watch for a
      // page change while the first decision runs
      const showP = round === 0 && !acted && !showWaited && missing ? (showWaited = true, tab.J("waitChange", page.at, SHOW_WAIT).catch(() => ({ changed: true }))) : null;
      let a = await this.decide(page, goal, values, hist(history.slice(-12)), lastChange, image);
      let act = this.resolve(page, a, values);
      if (showP) {
        const sure = act.tool !== "none" && (a.tool?.probabilities?.[act.tool] ?? 0) >= 0.8 && (!TARGETED.has(act.tool) || act.p_target >= 0.8) && Math.max(a.done?.noul ?? 0, a.done_change?.noul ?? 0) < 0.5;
        const w = sure ? await Promise.race([showP, sleep(0).then(() => null)]) : await showP;
        if (w?.changed || tab.loading) { await tab.settle({ cap: 2000 }); history.push({ event: `the page changed ${w?.waited ?? 0} ms into the first look; looked again` }); round--; continue; }
      }
      // the text decision is weak where an image can help: look again with the marked screenshot.
      // Unseen goal words count only before the first action: after one, a missing word is often
      // the result ("close the modal": the modal is gone)
      let textA = null;
      if (!image && ((TARGETED.has(act.tool) && act.p_target < 0.4) || (missing && !acted && (act.tool === "none" || (TARGETED.has(act.tool) && act.p_target < 0.6))))) {
        why = TARGETED.has(act.tool) && act.p_target < 0.4 ? "weak_target" : "unseen_words";
        image = await this.shot(page);
        if (image) { textA = a; a = await this.decide(page, goal, values, hist(history.slice(-12)), lastChange, image); act = this.resolve(page, a, values); }
      }
      prev = page;
      const n = (q) => a[q]?.noul ?? 0;
      // the image's done judge never ends a run before any action: a screenshot taken before
      // the page finished showing reads as "already done" (entry_ad with --vision ended likely_done)
      const done = image && !acted ? (textA ? Math.max(textA.done?.noul ?? 0, textA.done_change?.noul ?? 0) : 0) : Math.max(n("done"), n("done_change"));
      const r = { round, done: +done.toFixed(2), tool: act.tool, target: act.target, p_target: +act.p_target.toFixed(2), el: brief(act.el), irreversible: +n("irreversible").toFixed(2), login: +n("login").toFixed(2), error: +n("error").toFixed(2), candidates: act.candidates, ...(image ? { image: why } : {}), ...(missing ? { unseen: missing } : {}), ...(textA ? { text_pick: { tool: textA.tool?.choice, p_target: +(Object.values(textA.target?.probabilities ?? {}).sort((x, y) => y - x)[0] ?? 0).toFixed(2) } } : {}) };
      if (round > 0) r.changed = !!lastChange && !lastChange.nothing_changed;
      rounds.push(r);

      // a sign-in wall and no credentials: hand back before judging "done" (a redirect to a
      // login page often reads as "the page opened")
      if (n("login") >= 0.7 && !Object.keys(values).length && page.elements.some((e) => e.tag === "input:password") && !/\b(log ?in|sign ?in)\b/i.test(goal)) {
        status = "needs_login"; info = "the page wants a sign-in and no values were given; pass credentials in values, or log in by hand in the jevb profile"; break;
      }
      // DONE is never trusted alone: every ending goes through the `complete` confirm question
      // (asked at most once per round). done >= 0.5 (0.9 before any action) only triggers it.
      conf = null;
      // a scroll goal is not met while its named place is out of view, whatever the page has
      // (the kettle page's reviews below the fold got confirm 0.71 after two scrolls)
      const notThere = scrollWords && !inView(page, scrollWords);
      const arrived = NAV_ANY.test(goal) && arrivedAt(page, history);
      const arrivedInfo = `the last click opened the page its link "${String(arrived).slice(0, 60)}" named, and Jev sees nothing left to do`;
      const confirm = async () => {
        if (conf == null) {
          conf = r.confirm = await this.complete(page, goal, values, dialogs()).catch(() => 0);
          if (notThere) conf = r.confirm = Math.min(conf, 0.3);
        }
        return conf;
      };
      // a goal that ends in entering values ("enable the field, then type into it"): the page
      // alone rarely proves the entry was the goal, so the unsure band becomes done when Jev wants
      // no further action, rates done high, and code saw every value entered without error
      const lastAct = history.filter((h) => h.action && h.action !== "wait").at(-1);
      const enteredAll = Object.keys(values).length > 0 && lastAct && !lastAct.error && ["type", "select", "upload"].includes(lastAct.action)
        && Object.keys(values).every((k) => history.some((h) => h.value === k && !h.error));
      const settled = act.tool === "none" && done >= 0.7 && enteredAll;
      // a form submitted after every value was entered: code saw the page move to a new url
      // with no error, which the page-only confirm cannot see ("Login" on saucedemo: the
      // inventory page got confirm 0.22). A sign-in counts as done once the password field is gone.
      const lastEntry = history.filter((h) => ["type", "select", "upload"].includes(h.action)).at(-1);
      const submitted = Object.keys(values).length > 0 && Object.keys(values).every((k) => history.some((h) => h.value === k && !h.error))
        && lastAct && !lastAct.error && ["click", "press_enter"].includes(lastAct.action) && lastEntry && page.url !== lastEntry.url && n("error") < 0.3;
      const signedIn = submitted && /\b(log ?in|sign ?in)\b/i.test(goal) && !page.elements.some((e) => e.tag === "input:password") && n("login") < 0.3;
      // a search goal whose words were typed but never submitted (github-issue-search: typed
      // "flaky screenshot" and ended as done): press Enter in that field before any ending
      const searchEl = lastAct?.action === "type" && !lastAct.error && lastAct.url === page.url && SEARCHY_GOAL.test(goal)
        ? page.elements.find((e) => e.id === lastAct.id) : null;
      // Jev's own click on the Search button (or Enter) runs instead of an ending; anything else
      // (no action, a retype of the same field) becomes Enter in the field
      const pendingSearch = SEARCH_FIELD(searchEl) && act.tool !== "select" && !(act.tool === "type" && act.target !== searchEl.id);
      if (pendingSearch && !["click", "press_enter"].includes(act.tool)) {
        Object.assign(act, { tool: "press_enter", target: searchEl.id, el: searchEl, p_target: 1 });
        Object.assign(r, { tool: act.tool, target: act.target, el: brief(searchEl), forced_submit: true });
      }
      if (!pendingSearch && done >= (round > 0 ? 0.5 : 0.9)) {
        const c = await confirm();
        if (c >= 0.65 || (c >= 0.45 && settled)) { status = "done"; if (c < 0.65) info = "every given value was entered and Jev sees nothing left to do"; break; }
        if (c >= 0.45 && act.tool === "none" && arrived) { status = "done"; info = arrivedInfo; break; }
        if (c >= 0.45) { status = "likely_done"; info = UNSURE; break; }
      }
      if (n("login") >= 0.7 && !Object.keys(values).length) { status = "needs_login"; info = "the page wants a sign-in and no values were given; pass credentials in values, or log in by hand in the jevb profile"; break; }
      if (round > 0 && n("error") >= 0.7) { status = "error"; info = "the page shows an error after the last action"; break; }
      // the last action opened a JS dialog: never run it again or fall back to another action
      // (a repeat fires the dialog again); end with the judges, which now see the dialog
      if (lastAct?.dialog && !lastAct.error && (act.tool === "none" || (act.tool === lastAct.action && act.target === lastAct.id))) {
        const [c, ad] = await Promise.all([confirm(), this.actionDone(page, goal, history, lastChange).catch(() => 0)]);
        r.action_done = ad;
        if (c >= 0.65 || (ad >= 0.5 && done >= 0.5)) { status = "done"; info = `the ${lastAct.action} opened a dialog (${lastAct.dialog}) and Jev sees nothing left to do`; break; }
        if (c >= 0.45 || ad >= 0.5) { status = "likely_done"; info = `the ${lastAct.action} opened a dialog (${lastAct.dialog}), so it was not repeated; verify with check or observe`; break; }
        status = "stuck"; info = `the ${lastAct.action} on ${lastAct.element} opened a dialog (${lastAct.dialog}) but the goal does not look met; it was not repeated`; break;
      }
      // the goal names a page, tab or section that exists nowhere on this page (neg-missing-page:
      // "pricing" on Books to Scrape scrolled 3 times, 12 Jev calls): say so before scrolling
      if (NAV_GOAL.test(goal) && !history.some((h) => h.action && !/^scroll/.test(h.action))
        && (/^scroll/.test(act.tool) || act.tool === "none" || (TARGETED.has(act.tool) && act.p_target < 0.4))) {
        const missing = await this.missingNames(page, goal);
        if (missing) { status = "not_found"; info = `nothing on this page mentions ${missing.map((w) => `"${w}"`).join(", ")}, which the goal names; nothing was changed`; break; }
      }
      // no action on the first look: ask again only while the page is still loading
      if (act.tool === "none" && round === 0 && !retried && (tab.loading || !page.elements.length || page.elements.some((e) => e.busy) || [...tab.inflight.values()].some((s) => Date.now() - s < 2000))) {
        retried = true; await tab.waitChange(page, 1500).catch(() => {}); round--; rounds.pop(); continue;
      }
      if (act.tool === "none") {
        const c = await confirm();
        if (c >= 0.65) { status = "done"; break; }
        // a goal that opens a page, whose last click landed on the page its link named, and Jev wants no more
        if (arrived && (done >= 0.3 || (r.action_done = await this.actionDone(page, goal, history, lastChange).catch(() => 0)) >= 0.4)) { status = "done"; info = arrivedInfo; break; }
        if (c >= 0.45) { status = "likely_done"; info = UNSURE; break; }
        if (signedIn || (submitted && done >= 0.85)) { status = "done"; info = signedIn ? "signed in: the values were entered, the form was submitted and the sign-in form is gone" : "every given value was entered and the form was submitted to a new page without error"; break; }
        if (submitted && done >= 0.7) { status = "likely_done"; info = "every given value was entered and the form was submitted to a new page; verify with check or observe"; break; }
        // the goal is not met but Jev sees nothing to do: take its next-best action instead of stopping
        const alt = n("blocked") < 0.5 && fallbacks < 3 && Object.entries(a.tool?.probabilities ?? {}).filter(([t]) => t !== "none" && t !== "wait").sort((x, y) => y[1] - x[1])[0];
        const alts = alt && alt[1] >= 0.1 ? this.resolve(page, { ...a, tool: { choice: alt[0], probabilities: a.tool.probabilities } }, values) : null;
        if (!alts || (TARGETED.has(alts.tool) && (alts.target == null || alts.p_target < 0.3))) { status = n("blocked") >= 0.5 ? "blocked" : "stuck"; info = "Jev sees no next action and the goal does not look met"; break; }
        fallbacks++; Object.assign(act, alts); r.tool = act.tool; r.target = act.target; r.el = brief(act.el); r.fallback = true;
      }
      if (n("blocked") >= 0.85) { status = "blocked"; break; }
      if (act.tool === "wait") {
        if (++waits > MAX_WAITS || waitedMs >= WAIT_BUDGET) { status = "stuck"; info = `the page did not finish loading within ${Math.round(waitedMs / 1000)} s of waiting`; break; }
        const w = await tab.waitChange(page, Math.min(WAIT_STEP, WAIT_BUDGET - waitedMs));
        waitedMs += w.ms; r.waited = w.ms;
        history.push({ action: "wait", ...(w.changed ? {} : { note: `no change in ${Math.round(w.ms / 100) / 10} s` }) });
        round--; continue;   // waiting is not an action
      }
      // text to type that no value gives: hand back the field (Jev never writes text)
      if (act.tool === "type" && !Object.keys(values).length && FIELDISH(act.el)) {
        status = "needs_value"; gate = "value"; pending = { op: "type", id: act.target, element: brief(act.el) };
        info = `the next step types into ${brief(act.el)} and no value was given; pass the text in values (e.g. {"${valueKeyFor(act.el)}": "..."}) and run do again, or type it with act`;
        break;
      }
      // "scroll down to the X link and open it" with X in view: Jev keeps scrolling past it
      // (wiki: the lead's "Bletchley Park" scrolled away twice); do the goal's next action on it
      if (/^scroll/.test(act.tool) && act.el && !act.el.off && !act.el.hidden && act.p_target >= 0.8 && /\b(and|then)\b/i.test(goal)) {
        const alt = Object.entries(a.tool?.probabilities ?? {}).filter(([t]) => TARGETED.has(t) && t !== "type" && t !== "upload").sort((x, y) => y[1] - x[1])[0];
        if (alt && alt[1] >= 0.02) { act.tool = alt[0]; r.tool = act.tool; r.in_view = true; }
      }
      if (TARGETED.has(act.tool) && (act.target == null || act.p_target < 0.3)) { status = "ambiguous"; info = "target confidence too low; act on a candidate directly, or make the goal more specific"; break; }
      const key = `${act.tool}|${act.target}|${act.valueKey ?? ""}|${page.url}|${page.text}|${page.elements.length}`;
      seen.set(key, (seen.get(key) ?? 0) + 1);
      if (seen.get(key) >= 3) { status = "stuck"; info = "repeating the same action on the same page without progress"; break; }
      const seq = [...history.filter((h) => h.action).map((h) => `${h.action}|${h.element}|${h.value ?? ""}`), `${act.tool}|${brief(act.el)}|${["type", "select", "upload"].includes(act.tool) ? act.valueKey ?? "" : ""}`];
      if (repeatsBlock(seq, 2, 3) || repeatsBlock(seq, 3, 3) || repeatsBlock(seq, 1, 8)) { status = "stuck"; info = "repeating the same sequence of actions; the goal may already be done, check the page"; break; }
      // re-entering a value once every given value has been entered (without error) redoes the
      // task from the start (todomvc: re-added "buy milk" after clearing it); multi-step goals
      // rarely confirm from the page alone, so hand back as likely_done instead
      const entered = new Set(history.filter((h) => h.value && !h.error && ["type", "upload"].includes(h.action)).map((h) => h.value));
      if (["type", "upload"].includes(act.tool) && act.valueKey && entered.has(act.valueKey) && Object.keys(values).every((k) => entered.has(k))) {
        const c = await confirm();
        status = c >= 0.65 ? "done" : "likely_done";
        info = `every given value has been entered and Jev wanted to enter values.${act.valueKey} again; the goal may be done, so verify with check or observe`;
        break;
      }
      // toggles (sort headers, checkboxes, drag swaps) and repeats: before redoing the last
      // action on the same element, ask whether the goal is met so the repeat does not undo it.
      // action_done is asked only for non-clicks: for clicks it says yes to partial counts.
      const last = history.filter((h) => h.action).at(-1);
      if (last && !last.error && last.action === act.tool && last.element === brief(act.el) && !["type", "select", "upload", "wait"].includes(act.tool)) {
        const [c, ad] = await Promise.all([confirm(), act.tool === "click" ? 0 : this.actionDone(page, goal, history, lastChange).catch(() => 0)]);
        if (ad) r.action_done = ad;
        if (c >= 0.65) { status = "done"; info = `the goal looks met; did not ${last.action} ${last.element} again`; break; }
        if (ad >= 0.5 && !notThere) { status = "likely_done"; info = `the ${last.action} the goal asks for was done and changed the page, so it was not repeated; verify with check or observe`; break; }
      }

      // follow-up questions only for the chosen action
      if (act.tool === "select" && act.el?.options?.length) {
        // never commit an option the goal does not ask for: a missing option ends the run before
        // the page is touched (neg-missing-option: "Option 7" set the dropdown to "Option 2" twice)
        const opts = act.el.options, fromValue = act.value != null, words = fromValue ? String(act.value) : goal;
        const listed = `options: ${opts.map((o) => `"${o}"`).join(", ").slice(0, 400)}`;
        const notFound = (what) => { status = "not_found"; info = `the dropdown ${brief(act.el)} has no option ${what}; ${listed}`; };
        const v = (fromValue && opts.find((o) => o.toLowerCase() === words.toLowerCase())) || namedOption(opts, words, fromValue);
        const missing = !v && missingSibling(opts, words);
        if (v) act.value = v;
        else if (missing) { notFound(`"${missing}"`); break; }
        else {
          const crit = [...opts, NO_OPTION];
          const { answers } = await jev(scrub({ page: { url: page.url, title: page.title, text: page.text }, task: { goal, ...(Object.keys(values).length ? { values: redactValues(values) } : {}) }, dropdown: { label: act.el.label, options: opts } }, this.secrets), { opt: { type: "choice", instructions: `Which option of \`dropdown.options\` does \`task.goal\` ask for? Answer "${NO_OPTION}" when the goal asks for an option that is not in the list.`, criteria: Object.fromEntries(crit.map((o) => [o, null])) } });
          const pick = answers.opt?.choice;
          if (pick == null) { status = "ambiguous"; info = "could not pick a dropdown option"; break; }
          if (pick === NO_OPTION || (PLACEHOLDER_OPT.test(pick.trim()) && !/\b(reset|clear|placeholder|default|empty|none)\b/i.test(goal))) { notFound(fromValue ? "for the given value" : "matching the goal"); break; }
          act.value = pick; act.valueKey = undefined;
        }
      }
      // a select or type is a mutation: never run the same one twice in one do
      if (["select", "type"].includes(act.tool) && act.target != null) {
        const same = history.find((h) => !h.error && h.action === act.tool && h.id === act.target && (act.tool === "select" ? h.option === act.value : h.value === act.valueKey));
        if (same) {
          const c = await confirm();
          status = c >= 0.65 ? "done" : c >= 0.45 ? "likely_done" : "stuck";
          info = `${act.tool} on ${brief(act.el)} was already done in this run with the same ${act.tool === "select" ? "option" : "value"}, so it was not repeated${status === "done" ? "" : "; check the page"}`;
          break;
        }
      }
      if (act.tool === "press_key") {
        const { answers } = await jev(scrub({ page: { url: page.url, title: page.title, text: page.text, dialogs: page.dialogs }, task: { goal, history: hist(history.slice(-6)) } }, this.secrets), { key: { type: "choice", instructions: "Which keyboard key should be pressed next for `task.goal`?", criteria: Object.fromEntries(KEY_NAMES.map((k) => [k, null])) } });
        act.key = answers.key?.choice ?? "Escape";
        if (!act.el || !FIELDISH(act.el)) act.target = null;
      }
      if (act.tool === "drag") {
        const others = forJev(page).elements.filter((e) => e.i !== act.target).slice(0, MAX_SINGLE);
        if (!others.length) { status = "stuck"; info = "nothing on the page to drop onto"; break; }
        const { answers } = await jev(scrub({ page: { ...forJev(page), elements: others }, task: { goal, dragging: brief(act.el) } }, this.secrets), { dest: { type: "choice", instructions: "Onto which entry of `page.elements` (by its `i`) should `task.dragging` be dropped for `task.goal`?", criteria: Object.fromEntries(others.map((e) => [String(e.i), null])) } });
        act.to = answers.dest?.choice != null ? +answers.dest.choice : null;
        if (act.to == null) { status = "ambiguous"; info = "could not pick where to drop"; break; }
      }
      // irreversible gate: the code rule cannot be overridden by Jev; Jev can only add pauses
      if (GUARDED.has(act.tool) && !allow_irreversible) {
        const el = act.tool === "press_key" && act.target == null ? null : act.el;
        const focused = needsFocused(act.tool, act.target, act.key) ? await tab.J("focused").catch(() => null) : null;
        const rule = gateRule(act.tool, el, page, { key: act.key, focused });
        if (rule || n("irreversible") >= 0.6) {
          // the goal's form was already submitted to a new page: the gated click is a step past
          // the goal ("Fill in the checkout information and continue" then wanted "Finish")
          if (submitted && /\b(continue|submit|next|save|send|log ?in|sign ?in)\b/i.test(goal)) { status = "done"; info = `every given value was entered and the form was submitted to a new page; the next step ${brief(act.el)} is irreversible and was not run`; break; }
          status = "needs_confirmation"; info = `${rule || `Jev rates the next action as hard to undo (${n("irreversible").toFixed(2)})`}; call act with allow_irreversible, or do again with allow_irreversible, to proceed`;
          pending = { op: act.tool, id: act.target, element: brief(act.el), ...(act.key ? { key: act.key } : {}) };
          gate = rule ? "rule" : "jev";
          break;
        }
      }
      // budget reached: run this one action anyway (up to EXTRA_ROUNDS more) when it is a sure
      // step on a page that is still moving; otherwise hand it back as `next` for one act call
      // `deadline` (the Haiku loop's wall-clock budget; unset otherwise) ends the run the same way
      const late = deadline != null && Date.now() >= deadline;
      if (round >= limit || late) {
        const sure = !late && act.tool !== "none" && (a.tool?.probabilities?.[act.tool] ?? 0) >= 0.8 && (!TARGETED.has(act.tool) || act.p_target >= 0.8)
          && (seen.get(key) ?? 0) < 2 && !(repeatsBlock(seq, 2, 2) || repeatsBlock(seq, 3, 2) || repeatsBlock(seq, 1, 4))
          && rounds.slice(-2).some((x) => x.changed);
        if (sure && limit < max_actions + EXTRA_ROUNDS) { limit++; r.over_budget = true; }
        else {
          pending = { op: act.tool, id: act.target, element: brief(act.el), ...(act.valueKey && ["type", "select", "upload"].includes(act.tool) ? { value_key: act.valueKey } : {}), ...(act.tool === "select" && act.value != null ? { option: act.value } : {}), ...(act.key ? { key: act.key } : {}), ...(act.to != null ? { to: act.to } : {}) };
          gate = "budget"; if (late) info = "the call's time budget ran out"; break;
        }
      }
      const h = { action: act.tool, element: brief(act.el), id: act.target, url: page.url, tag: act.el?.tag, name: act.el?.text || act.el?.label };
      if (act.tool === "select" && act.value != null) h.option = act.value;
      if (act.valueKey && ["type", "select", "upload"].includes(act.tool)) h.value = act.valueKey;
      if (act.key) h.key = act.key;
      if (act.to != null) h.destination = brief(page.elements.find((e) => e.id === act.to));
      tab.held = null;
      const since = Date.now();
      try {
        let op = act.tool === "scroll_down" || act.tool === "scroll_up" ? "scroll" : act.tool, note;
        // a scroll toward a place the page already has goes there in one step: the off-screen
        // element Jev targets and the goal names (wiki: "Bletchley Park" took 7 wheel steps of 640 px), or the
        // heading a scroll goal names; else (not loaded yet) one wheel step
        if (op === "scroll" && act.el?.off && !act.el.hidden && act.p_target >= 0.5 && goalNames(act.el, goal)) note = await tab.exec({ op: "reveal", id: act.target });
        else if (op === "scroll" && notThere) note = await tab.exec({ op: "reveal", value: scrollWords.map(stem) }).catch(() => null);
        note ??= await tab.exec({ op, id: act.target ?? undefined, value: act.value, key: act.key, to: act.to, direction: act.tool === "scroll_up" ? "up" : "down" });
        if (note) h.note = note;
        if (op === "type" && act.valueKey) this.typed.set(act.target, act.valueKey);
        await tab.settle({ since });
        if (!allow_irreversible) await tab.takeHeld();
      } catch (e) { h.error = e.message.slice(0, 160); }
      const dl = tab.dialogsSince(since);
      if (dl.length) h.dialog = dl.join("; ");
      history.push(h);
      if (tab.held?.submit) { status = "needs_confirmation"; info = heldInfo(tab.held); pending = { op: h.action, id: act.target, element: h.element, ...(act.key ? { key: act.key } : {}) }; gate = "submit"; break; }
      if (tab.held) { status = "needs_confirmation"; info = "the action opened a confirmation dialog that looks irreversible, so it was dismissed; repeat with allow_irreversible to accept it"; pending = { op: h.action, id: act.target, element: h.element, dialog: tab.held.message }; break; }
      const u = await tab.url();
      if (!domainAllowed(u)) { await tab.exec({ op: "back" }).catch(() => {}); status = "blocked"; info = `navigation to ${u} is outside JEVB_ALLOWED_DOMAINS`; break; }
    }
    // stuck, max_actions or ambiguous after acting: the goal may be met anyway (repeat
    // detectors fire on finished toggles and scrolls); ask before handing back as a failure
    if (["stuck", "max_actions", "ambiguous"].includes(status) && history.some((h) => h.action) && page) {
      // judge the page as it is now: after a wait (or a decision that took a while) the
      // round's snapshot can be older than what finally rendered
      const fresh = await tab.snapshot().catch(() => null);
      if (fresh) {
        if (fresh.text !== page.text || fresh.elements.length !== page.elements.length || fresh.url !== page.url) { lastChange = withDialog(diffForJev(pageDiff(prev ?? page, fresh)), history.filter((h) => h.action && h.action !== "wait").at(-1)); conf = null; if (rounds.length) rounds.at(-1).action_done = undefined; }
        page = fresh;
      }
      const r0 = rounds.at(-1);
      // in parallel: the page-only confirm, and whether the action the goal names (drag, scroll…)
      // was done. Goals that name an action cannot be confirmed from the page alone, but the
      // second question also says yes to partial progress, so it only earns likely_done.
      let [c, ad] = await Promise.all([
        conf ?? this.complete(page, goal, values, dialogs()).catch(() => 0),
        r0 && r0.action_done != null ? r0.action_done : this.actionDone(page, goal, history, lastChange).catch(() => 0),
      ]);
      if (scrollWords && !inView(page, scrollWords)) { c = Math.min(c, 0.3); ad = Math.min(ad, 0.3); }
      conf = c;
      if (rounds.length) Object.assign(rounds.at(-1), { confirm: c, action_done: ad });
      const why = status === "max_actions" ? "the action budget ran out" : status === "ambiguous" ? "the next target was unclear" : "a repeat or dead end was detected";
      const at = NAV_ANY.test(goal) && arrivedAt(page, history);
      if (c >= 0.65) { info = `${why} but the goal looks met`; status = "done"; }
      else if (at && ad >= 0.4) { info = `${why}, but the last click opened the page its link "${String(at).slice(0, 60)}" named`; status = "done"; }
      else if (c >= 0.45) { info = `${why} but the goal may be met; verify with check or observe`; status = "likely_done"; }
      else if (ad >= 0.5) { info = `${why}, but the action the goal asks for was done and changed the page; verify with check or observe`; status = "likely_done"; }
    }
    for (const e of this.events()) history.push({ event: e });
    const final = await tab.snapshot().catch(() => page);
    this.shown = final;
    const lines = [`status: ${status}`];
    if (info) lines.push(`info: ${info}`);
    if (pending && gate === "budget") { if (status !== "done") lines.push(`next: ${pending.op}${pending.id != null ? ` [${pending.id}]` : ""} ${pending.element}${pending.value_key ? ` <- values.${pending.value_key}` : ""}${pending.option ? ` = "${pending.option}"` : ""}${pending.key ? ` key=${pending.key}` : ""}${pending.to != null ? ` -> [${pending.to}]` : ""} (the planned action when the budget ran out; run it with act)`); }
    else if (pending && gate === "value") lines.push(`field: [${pending.id}] ${pending.element}`);
    else if (pending) lines.push(`pending: ${pending.op}${pending.id != null ? ` [${pending.id}]` : ""}${pending.key ? ` key=${pending.key}` : ""} ${pending.element}${pending.dialog ? ` dialog "${pending.dialog}"` : ""} (gate: ${gate ?? "dialog"})`, resumeLine(pending));
    lines.push(`actions (${history.filter((h) => h.action).length}, ${stats.calls - calls0} Jev calls, ${Date.now() - t0} ms):`);
    for (const h of history) lines.push(h.event ? `  ~ ${h.event}` : `  - ${h.action}${h.element ? ` ${h.element}` : ""}${h.value ? ` <- values.${h.value}` : ""}${h.option ? ` = "${h.option}"` : ""}${h.key ? ` key=${h.key}` : ""}${h.destination ? ` -> ${h.destination}` : ""}${h.note ? ` (${h.note})` : ""}${h.dialog ? ` [dialog: ${h.dialog}]` : ""}${h.error ? ` ERROR ${h.error}` : ""}`);
    if (["ambiguous", "stuck", "max_actions"].includes(status) && rounds.at(-1)?.candidates) lines.push(`candidates: ${rounds.at(-1).candidates.map((c) => `[${c.id}] ${c.el} p=${c.p}`).join("; ")}`);
    // done: a short view of the result; otherwise the full page so Claude can take over at once
    const view = ["done", "likely_done"].includes(status)
      ? `url: ${final.url}\ntitle: ${final.title}\n${fence(`visible text: ${final.text.slice(0, 700)}`)}`
      : renderPage(final, { maxChars: 9000 });
    return this.out(`${lines.join("\n")}\n${view}`, { status, info, pending, rounds, ms: Date.now() - t0, jev_calls: stats.calls - calls0 });
  }
}
