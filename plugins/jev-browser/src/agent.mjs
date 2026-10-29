// The five operations (open, do, act, observe, check) plus eval, over one Tab.
// Each returns { text, image?, data } where `text` is what Claude reads.
import { jev, stats } from "./jev.mjs";
import { KEY_NAMES } from "./tab.mjs";
import { brief, renderPage, renderDiff, pageDiff, diffForJev } from "./format.mjs";
import { fence, scrub, secretsOf, redactValues, irreversibleRule, dialogRisky, domainAllowed, REDACTED } from "./safety.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MAX_SINGLE = 240, GROUP = 30, MAX_STATE_CHARS = 70_000;

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

// Jev sees elements keyed by `i`; Claude sees `[id]`. Same number. `typed` maps element id ->
// the values key typed into it, so a filled password field shows which value it holds (never the value).
function repeated(elements) {
  const counts = {};
  for (const e of elements) { const k = brief(e); counts[k] = (counts[k] ?? 0) + 1; }
  const rep = Object.entries(counts).filter(([, n]) => n > 1).sort((a, b) => b[1] - a[1]).slice(0, 10);
  return rep.length ? Object.fromEntries(rep) : undefined;
}
const forJev = (page, typed) => ({
  url: page.url, title: page.title, text: page.text, dialogs: page.dialogs?.length ? page.dialogs : undefined, metrics: page.metrics,
  repeated_elements: repeated(page.elements),
  elements: page.elements.map(({ id, new: _n, ...e }) => ({ i: id, ...e, ...(e.value === "[filled]" && typed?.has(id) ? { value: `[filled with values.${typed.get(id)}]` } : {}) })),
});

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

  async open({ url, viewport }) {
    if (!/^[a-z]+:/i.test(url)) url = `https://${url}`;
    if (!domainAllowed(url)) return this.out(`status: blocked\ninfo: ${url} is outside JEVB_ALLOWED_DOMAINS`, { status: "blocked" });
    const t0 = Date.now();
    await this.tab.navigate(url);
    const page = await this.tab.snapshot();
    this.shown = page;
    const ev = this.events();
    return this.out(`opened in ${Date.now() - t0} ms${ev.length ? `\nevents: ${ev.join("; ")}` : ""}${page.captcha ? "\nwarning: the page shows a captcha or bot check (needs_human)" : ""}\n${renderPage(page)}`, { url: page.url, title: page.title, elements: page.elements.length, ms: Date.now() - t0 });
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
    const image = vision ? await this.tab.screenshot() : undefined;
    const st = scrub({ page: forJev(page) }, this.secrets);
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
    const results = []; let status = "ok", pending, info;
    const url0 = await this.tab.url();
    for (const [n, a] of actions.entries()) {
      const op = a.op;
      const value = a.value_key != null ? values[a.value_key] : a.value;
      if (a.value_key != null && value === undefined) { status = "error"; results.push({ op, id: a.id, error: `no value with key "${a.value_key}"` }); break; }
      const el = byId.get(Number(a.id));
      const rule = GUARDED.has(op === "dblclick" ? "click" : op) && irreversibleRule(el, before);
      if (rule && !allow_irreversible) { status = "needs_confirmation"; pending = { op, id: a.id, element: brief(el) }; info = `${rule}; repeat with allow_irreversible to do it`; break; }
      this.tab.dialogPolicy = (d) => (allow_irreversible || !dialogRisky(d.message) ? { accept: true, text: values.prompt } : { accept: false, held: true });
      this.tab.held = null;
      const since = Date.now();
      try {
        const note = await this.tab.exec({ op, id: a.id != null ? Number(a.id) : undefined, value, key: a.key, to: a.to != null ? Number(a.to) : undefined, direction: a.direction });
        await this.tab.settle({ since });
        results.push({ op, id: a.id, ok: true, ...(note ? { note } : {}) });
      } catch (e) {
        results.push({ op, id: a.id, error: e.message.slice(0, 200) }); status = "error"; break;
      }
      if (this.tab.held) { status = "needs_confirmation"; info = `a "${this.tab.held.message.slice(0, 80)}" dialog looked irreversible and was dismissed; repeat with allow_irreversible`; break; }
      const u = await this.tab.url();
      if (!domainAllowed(u)) { await this.tab.exec({ op: "back" }).catch(() => {}); status = "blocked"; info = `navigation to ${u} is outside JEVB_ALLOWED_DOMAINS`; break; }
      if (u !== url0 && n < actions.length - 1) { info = `the page navigated after action ${n + 1}; the remaining ${actions.length - n - 1} were not run`; break; }
    }
    const page = await this.tab.snapshot();
    const ev = this.events();
    const navigated = page.url.split("#")[0] !== before.url.split("#")[0];
    const view = navigated ? renderPage(page) : renderDiff(page, pageDiff(before, page));
    this.shown = page;
    const lines = [`status: ${status}`, ...results.map((r) => `- ${r.op}${r.id != null ? ` [${r.id}]` : ""}: ${r.error ? `ERROR ${r.error}` : `ok${r.note ? ` (${r.note})` : ""}`}`)];
    if (info) lines.push(`info: ${info}`);
    if (pending) lines.push(`pending: ${pending.op} [${pending.id}] ${pending.element}`);
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
    if (!hasValues) { delete tools.type; delete tools.upload; }
    if (!page.elements.some(FILEISH)) delete tools.upload;
    if (!page.elements.some(SELECTISH)) delete tools.select;
    const common = {
      done: { type: "noul", instructions: `Does \`page\` show that \`task.goal\` has been achieved${withValues}? Judge from \`page.text\` and \`page.elements\`.` },
      done_change: { type: "noul", instructions: `Does \`page\` show that \`task.goal\` has been achieved${withValues}? Judge from \`page.text\`, \`page.elements\` and \`task.last_change\` (what the last action changed).` },
      blocked: { type: "noul", instructions: "Is there something on `page` that stops progress on `task.goal` and cannot be handled by clicking or typing (captcha, access denied, error page)?" },
      error: { type: "noul", instructions: "Does `page` show an error or rejection message (e.g. invalid credentials, a validation error, not found) caused by the actions in `task.history`?" },
      login: { type: "noul", instructions: "Is `page` a sign-in or sign-up screen, or asking the user to log in, before `task.goal` can continue?" },
      irreversible: { type: "noul", instructions: "Would the next action toward `task.goal` on `page` have an effect outside this browser that is hard to undo, such as placing an order, paying, sending a message, deleting data or publishing?" },
      tool: { type: "choice", instructions: "What is the next action toward `task.goal` on `page`, given what `task.history` already did?", criteria: tools },
    };
    if (hasValues) common.value = { type: "choice", instructions: "If the next action toward `task.goal` types, selects or uploads something, which of `task.values` should it use? Prefer values not yet entered on `page`.", criteria: Object.fromEntries(Object.entries(shownValues).map(([k, v]) => [k, v])) };
    const targetQ = { type: "choice", instructions: "Which entry of `page.elements` (by its `i`) should the next action toward `task.goal` act on?" };
    const jp = forJev(page, this.typed);
    if (image) targetQ.instructions += " The image `screenshot` shows the page.";
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
    if (tool === "type" && !Object.keys(values).length) tool = "click";
    let valueKey = a.value?.choice;
    if (valueKey == null && Object.keys(values).length === 1) valueKey = Object.keys(values)[0];
    return {
      tool, p_tool: a.tool?.probabilities?.[tool] ?? 0, target: ti == null ? null : +ti, p_target: tp ?? 0, el: byI.get(ti),
      valueKey, value: valueKey != null ? values[valueKey] : undefined,
      candidates: ranked.slice(0, 3).map(([i, p]) => ({ id: +i, p: +p.toFixed(2), el: brief(byI.get(i)) })),
    };
  }

  async do({ goal, values = {}, max_actions = 10, allow_irreversible = false, vision = false }) {
    const t0 = Date.now(), calls0 = stats.calls, tab = this.tab;
    max_actions = Math.min(Math.max(1, max_actions | 0 || 10), 30);
    this.remember(values);
    const history = [], rounds = [];
    let prev = null, page = null, waits = 0, status = "max_actions", info, pending, retried = false, gate;
    const seen = new Map();
    tab.dialogPolicy = (d) => (allow_irreversible || !dialogRisky(d.message) ? { accept: true, text: values.prompt } : { accept: false, held: true });
    for (let round = 0; round <= max_actions; round++) {
      page = await tab.snapshot();
      for (const e of this.events()) history.push({ event: e });
      if (page.captcha) { status = "needs_human"; info = "the page shows a captcha or bot check; a person has to solve it"; break; }
      const useImage = vision || page.elements.length === 0;
      const image = useImage ? await tab.screenshot().catch(() => undefined) : undefined;
      const a = await this.decide(page, goal, values, history.slice(-12), round ? diffForJev(pageDiff(prev, page)) : undefined, image);
      prev = page;
      const act = this.resolve(page, a, values);
      const n = (q) => a[q]?.noul ?? 0;
      const done = Math.max(n("done"), n("done_change"));
      const r = { round, done: +done.toFixed(2), tool: act.tool, target: act.target, p_target: +act.p_target.toFixed(2), el: brief(act.el), irreversible: +n("irreversible").toFixed(2), login: +n("login").toFixed(2), error: +n("error").toFixed(2), candidates: act.candidates };
      rounds.push(r);

      // a sign-in wall and no credentials: hand back before judging "done" (a redirect to a
      // login page often reads as "the page opened")
      if (n("login") >= 0.7 && !Object.keys(values).length && page.elements.some((e) => e.tag === "input:password") && !/\b(log ?in|sign ?in)\b/i.test(goal)) {
        status = "needs_login"; info = "the page wants a sign-in and no values were given; pass credentials in values, or log in by hand in the jevb profile"; break;
      }
      if (round > 0 ? done >= 0.5 && done < 0.85 && act.tool !== "none" : done >= 0.9) {
        const c = await jev(scrub({ page: forJev(page, this.typed), task: { goal, history: history.slice(-12) } }, this.secrets), { complete: { type: "noul", instructions: "Is everything `task.goal` asks for already finished on `page`, so that no further action (such as pressing a submit, search or continue button) is needed?" } });
        r.confirm = +(c.answers.complete?.noul ?? 0).toFixed(2);
        if (r.confirm >= 0.65) { status = "done"; break; }
        if (r.confirm >= 0.45) { status = "likely_done"; info = "the page looks done but Jev is unsure; verify with check or observe"; break; }
      } else if (done >= (round > 0 ? 0.5 : 0.9) && (done >= 0.85 || act.tool === "none" || round === 0)) { status = "done"; break; }
      if (round === max_actions) break;
      if (n("login") >= 0.7 && !Object.keys(values).length) { status = "needs_login"; info = "the page wants a sign-in and no values were given; pass credentials in values, or log in by hand in the jevb profile"; break; }
      if (round > 0 && n("error") >= 0.7) { status = "error"; info = "the page shows an error after the last action"; break; }
      if (act.tool === "none" && round === 0 && !retried) { retried = true; await sleep(1000); round--; rounds.pop(); continue; }
      if (act.tool === "none" && round > 0 && done >= 0.35 && n("error") < 0.5 && n("blocked") < 0.5) { status = "likely_done"; info = "no further action seems needed but Jev is unsure the goal is met; verify with check or observe"; break; }
      if (act.tool === "none") { status = n("blocked") >= 0.5 ? "blocked" : "stuck"; break; }
      if (n("blocked") >= 0.85) { status = "blocked"; break; }
      if (act.tool === "wait") {
        if (++waits > 8) { status = "stuck"; info = "the page never finished loading"; break; }
        await sleep(400); await tab.settle({ cap: 2000 }); history.push({ action: "wait" }); continue;
      }
      if (TARGETED.has(act.tool) && (act.target == null || act.p_target < 0.3)) { status = "ambiguous"; info = "target confidence too low; act on a candidate directly, or make the goal more specific"; break; }
      const key = `${act.tool}|${act.target}|${act.valueKey ?? ""}|${page.url}|${page.text}|${page.elements.length}`;
      seen.set(key, (seen.get(key) ?? 0) + 1);
      if (seen.get(key) >= 3) { status = "stuck"; info = "repeating the same action on the same page without progress"; break; }
      const seq = [...history.filter((h) => h.action).map((h) => `${h.action}|${h.element}|${h.value ?? ""}`), `${act.tool}|${brief(act.el)}|${["type", "select", "upload"].includes(act.tool) ? act.valueKey ?? "" : ""}`];
      if (repeatsBlock(seq, 2, 3) || repeatsBlock(seq, 3, 3) || repeatsBlock(seq, 1, 8)) { status = "stuck"; info = "repeating the same sequence of actions; the goal may already be done, check the page"; break; }

      // follow-up questions only for the chosen action
      if (act.tool === "select" && act.el?.options?.length) {
        const opts = act.el.options;
        const v = act.value != null && opts.find((o) => o.toLowerCase() === String(act.value).toLowerCase());
        if (v) act.value = v;
        else {
          const { answers } = await jev(scrub({ page: { url: page.url, title: page.title, text: page.text }, task: { goal, ...(Object.keys(values).length ? { values: redactValues(values) } : {}) }, dropdown: { label: act.el.label, options: opts } }, this.secrets), { opt: { type: "choice", instructions: "Which option of `dropdown.options` should be chosen for `task.goal`?", criteria: Object.fromEntries(opts.map((o) => [o, null])) } });
          act.value = answers.opt?.choice; act.valueKey = undefined;
          if (act.value == null) { status = "ambiguous"; info = "could not pick a dropdown option"; break; }
        }
      }
      if (act.tool === "press_key") {
        const { answers } = await jev(scrub({ page: { url: page.url, title: page.title, text: page.text, dialogs: page.dialogs }, task: { goal, history: history.slice(-6) } }, this.secrets), { key: { type: "choice", instructions: "Which keyboard key should be pressed next for `task.goal`?", criteria: Object.fromEntries(KEY_NAMES.map((k) => [k, null])) } });
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
        const rule = act.tool !== "press_key" && irreversibleRule(act.el, page);
        if (rule || n("irreversible") >= 0.6) {
          status = "needs_confirmation"; info = `${rule || `Jev rates the next action as hard to undo (${n("irreversible").toFixed(2)})`}; call act with allow_irreversible, or do again with allow_irreversible, to proceed`;
          pending = { op: act.tool, id: act.target, element: brief(act.el), ...(act.key ? { key: act.key } : {}) };
          gate = rule ? "rule" : "jev";
          break;
        }
      }
      const h = { action: act.tool, element: brief(act.el) };
      if (act.tool === "select" && act.value != null) h.option = act.value;
      if (act.valueKey && ["type", "select", "upload"].includes(act.tool)) h.value = act.valueKey;
      if (act.key) h.key = act.key;
      if (act.to != null) h.destination = brief(page.elements.find((e) => e.id === act.to));
      tab.held = null;
      const since = Date.now();
      try {
        const op = act.tool === "scroll_down" || act.tool === "scroll_up" ? "scroll" : act.tool;
        const note = await tab.exec({ op, id: act.target ?? undefined, value: act.value, key: act.key, to: act.to, direction: act.tool === "scroll_up" ? "up" : "down" });
        if (note) h.note = note;
        if (op === "type" && act.valueKey) this.typed.set(act.target, act.valueKey);
        await tab.settle({ since });
      } catch (e) { h.error = e.message.slice(0, 160); }
      history.push(h);
      if (tab.held) { status = "needs_confirmation"; info = "the action opened a confirmation dialog that looks irreversible, so it was dismissed; repeat with allow_irreversible to accept it"; pending = { op: h.action, id: act.target, element: h.element, dialog: tab.held.message }; break; }
      const u = await tab.url();
      if (!domainAllowed(u)) { await tab.exec({ op: "back" }).catch(() => {}); status = "blocked"; info = `navigation to ${u} is outside JEVB_ALLOWED_DOMAINS`; break; }
    }
    for (const e of this.events()) history.push({ event: e });
    const final = await tab.snapshot().catch(() => page);
    this.shown = final;
    const lines = [`status: ${status}`];
    if (info) lines.push(`info: ${info}`);
    if (pending) lines.push(`pending: ${pending.op}${pending.id != null ? ` [${pending.id}]` : ""} ${pending.element}${pending.dialog ? ` dialog "${pending.dialog}"` : ""} (gate: ${gate ?? "dialog"})`);
    lines.push(`actions (${history.filter((h) => h.action).length}, ${stats.calls - calls0} Jev calls, ${Date.now() - t0} ms):`);
    for (const h of history) lines.push(h.event ? `  ~ ${h.event}` : `  - ${h.action} ${h.element}${h.value ? ` <- values.${h.value}` : ""}${h.option ? ` = "${h.option}"` : ""}${h.key ? ` key=${h.key}` : ""}${h.destination ? ` -> ${h.destination}` : ""}${h.note ? ` (${h.note})` : ""}${h.error ? ` ERROR ${h.error}` : ""}`);
    if (["ambiguous", "stuck", "max_actions"].includes(status) && rounds.at(-1)?.candidates) lines.push(`candidates: ${rounds.at(-1).candidates.map((c) => `[${c.id}] ${c.el} p=${c.p}`).join("; ")}`);
    // done: a short view of the result; otherwise the full page so Claude can take over at once
    const view = ["done", "likely_done"].includes(status)
      ? `url: ${final.url}\ntitle: ${final.title}\n${fence(`visible text: ${final.text.slice(0, 700)}`)}`
      : renderPage(final, { maxChars: 9000 });
    return this.out(`${lines.join("\n")}\n${view}`, { status, info, pending, rounds, ms: Date.now() - t0, jev_calls: stats.calls - calls0 });
  }
}
