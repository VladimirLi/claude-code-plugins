// One session = one background tab (plus any pop-up it opens). Holds the CDP session, tracks
// network and navigation for the settler, handles dialogs, and executes actions with CDP Input
// at fresh coordinates after a hit test.
import { INSTALL, VERSION, MUTATION_WATCH } from "./page.mjs";
import { dialogRisky } from "./safety.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now();
export const VIEWPORT = { width: 1280, height: 800 };
const SNAP = `(${INSTALL.toString()})(${VERSION})`;

const KEYS = {
  Enter: { code: "Enter", vk: 13, text: "\r" }, Escape: { code: "Escape", vk: 27 }, Tab: { code: "Tab", vk: 9 },
  Space: { key: " ", code: "Space", vk: 32, text: " " }, Backspace: { code: "Backspace", vk: 8 }, Delete: { code: "Delete", vk: 46 },
  ArrowDown: { code: "ArrowDown", vk: 40 }, ArrowUp: { code: "ArrowUp", vk: 38 }, ArrowLeft: { code: "ArrowLeft", vk: 37 }, ArrowRight: { code: "ArrowRight", vk: 39 },
  PageDown: { code: "PageDown", vk: 34 }, PageUp: { code: "PageUp", vk: 33 }, Home: { code: "Home", vk: 36 }, End: { code: "End", vk: 35 },
};
export const KEY_NAMES = Object.keys(KEYS);

export class Browser {
  constructor(cdp, proc) {
    this.cdp = cdp; this.proc = proc; this.tabs = new Map(); this.byTarget = new Map(); this.bySession = new Map();
    cdp.on((method, params, sessionId) => {
      if (sessionId) return this.bySession.get(sessionId)?.event(method, params);
      if (method === "Target.targetCreated" && params.targetInfo.type === "page" && params.targetInfo.openerId) {
        this.byTarget.get(params.targetInfo.openerId)?.popup(params.targetInfo);
      }
      if (method === "Target.targetDestroyed") this.byTarget.get(params.targetId)?.destroyed(params.targetId);
      if (method === "Target.detachedFromTarget") this.bySession.get(params.sessionId)?.detached(params.sessionId);
    });
  }
  async init() {
    await this.cdp.send("Target.setDiscoverTargets", { discover: true });
    const v = await this.cdp.send("Browser.getVersion");
    this.ua = v.userAgent.replace("HeadlessChrome", "Chrome");
  }
  get alive() { return !this.cdp.closed; }
  // fresh: the session gets its own browser context (no cookies or storage from the profile,
  // nothing kept after close). Otherwise it shares the dedicated profile.
  async tab(name, { fresh = false } = {}) {
    let t = this.tabs.get(name);
    if (t && !t.dead) { await t.ready; return t; }
    t = new Tab(this, name);
    this.tabs.set(name, t);
    t.ready = (async () => {
      if (fresh) t.contextId = (await this.cdp.send("Target.createBrowserContext", { disposeOnDetach: false })).browserContextId;
      await t.create();
    })();
    try { await t.ready; } catch (e) {
      this.tabs.delete(name);
      for (const id of t.targets) this.cdp.send("Target.closeTarget", { targetId: id }).catch(() => {});
      throw e;
    }
    return t;
  }
  async close(name) {
    const t = this.tabs.get(name); if (!t) return false;
    this.tabs.delete(name);
    for (const id of t.targets) await this.cdp.send("Target.closeTarget", { targetId: id }).catch(() => {});
    if (t.contextId) await this.cdp.send("Target.disposeBrowserContext", { browserContextId: t.contextId }).catch(() => {});
    return true;
  }
}

export class Tab {
  constructor(browser, name) {
    this.b = browser; this.cdp = browser.cdp; this.name = name;
    this.targets = []; this.targetId = null; this.sid = null;
    this.inflight = new Map(); this.events = []; this.dialogs = []; this.dialogAt = 0; this.loading = false; this.dclAt = 0; this.navAt = 0;
    this.nextId = 1; this.chain = Promise.resolve(); this.dead = false;
    this.dialogPolicy = () => ({ accept: true });
    this.held = null;
  }

  // Serialise everything one session does; different sessions run in parallel.
  run(fn) { const p = this.chain.then(fn, fn); this.chain = p.catch(() => {}); return p; }

  async create() {
    const { targetId } = await this.cdp.send("Target.createTarget", { url: "about:blank", newWindow: true, ...(this.contextId ? { browserContextId: this.contextId } : {}) });
    await this.attach(targetId);
  }

  async attach(targetId) {
    const { sessionId } = await this.cdp.send("Target.attachToTarget", { targetId, flatten: true });
    if (this.sid) this.b.bySession.delete(this.sid);
    this.targetId = targetId; this.sid = sessionId;
    if (!this.targets.includes(targetId)) this.targets.push(targetId);
    this.b.byTarget.set(targetId, this); this.b.bySession.set(sessionId, this);
    this.inflight.clear(); this.loading = false;
    const s = (m, p) => this.cdp.send(m, p, sessionId);
    await Promise.all([
      s("Page.enable"), s("Network.enable", { maxTotalBufferSize: 0, maxResourceBufferSize: 0 }),
      s("Page.addScriptToEvaluateOnNewDocument", { source: MUTATION_WATCH }),
      s("Emulation.setFocusEmulationEnabled", { enabled: true }),
      s("Emulation.setDeviceMetricsOverride", { ...VIEWPORT, deviceScaleFactor: 1, mobile: false }),
      s("Network.setUserAgentOverride", { userAgent: this.b.ua, acceptLanguage: "en-US,en" }),
      s("Runtime.runIfWaitingForDebugger"),
    ]);
    await this.eval(MUTATION_WATCH).catch(() => {});
  }

  send(method, params = {}, timeout) { return this.cdp.send(method, params, this.sid, timeout); }

  event(method, p) {
    switch (method) {
      case "Network.requestWillBeSent":
        if (["Document", "XHR", "Fetch"].includes(p.type)) this.inflight.set(p.requestId, now());
        break;
      case "Network.loadingFinished": case "Network.loadingFailed": this.inflight.delete(p.requestId); break;
      case "Page.frameStartedLoading": if (p.frameId === this.targetId) { this.loading = true; this.navAt = now(); } break;
      case "Page.frameNavigated": if (!p.frame.parentId) this.navAt = now(); break;
      case "Page.domContentEventFired": this.loading = false; this.dclAt = now(); break;
      case "Page.frameStoppedLoading": if (p.frameId === this.targetId) this.loading = false; break;
      case "Page.javascriptDialogOpening": {
        const d = { type: p.type, message: String(p.message || "").slice(0, 300) };
        const pol = this.dialogPolicy(d);
        this.send("Page.handleJavaScriptDialog", { accept: pol.accept, ...(p.type === "prompt" ? { promptText: pol.text ?? p.defaultPrompt ?? "" } : {}) }).catch(() => {});
        this.events.push(`${d.type} dialog "${d.message.slice(0, 100)}" ${pol.accept ? "accepted" : "dismissed"}`);
        // a dialog is a page reaction the DOM never shows: the settler and the ending judges
        // must see it as a change (context-menu: the alert was the whole result of right_click)
        this.dialogAt = now(); this.dialogs.push({ ...d, accepted: pol.accept, at: this.dialogAt }); if (this.dialogs.length > 20) this.dialogs.shift();
        if (!pol.accept && pol.held) this.held = d;
        break;
      }
    }
  }

  // A page opened by this tab (target=_blank, window.open) becomes the session's tab.
  async popup(info) {
    this.events.push(`new tab opened: ${info.url || "(loading)"}`);
    try { await this.attach(info.targetId); this.loading = true; this.navAt = now(); } catch {}
  }
  destroyed(targetId) {
    this.targets = this.targets.filter((t) => t !== targetId);
    if (targetId !== this.targetId) return;
    const prev = this.targets.at(-1);
    if (prev) { this.events.push("tab closed; back to the previous tab"); this.attach(prev).catch(() => { this.dead = true; }); }
    else this.dead = true;
  }
  detached(sessionId) { if (sessionId === this.sid && !this.targets.length) this.dead = true; }

  async eval(expression, { timeout = 15_000, await: aw = true } = {}) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: aw, userGesture: true }, timeout);
    if (r.exceptionDetails) throw new Error(`page script: ${r.exceptionDetails.exception?.description?.split("\n")[0] ?? r.exceptionDetails.text}`);
    return r.result.value;
  }
  // Call a method of window.__jevb, installing it first if this document has none.
  async J(method, ...args) {
    const a = args.map((x) => JSON.stringify(x ?? null)).join(",");
    for (let i = 0; ; i++) {
      try { return await this.eval(`${SNAP}.${method}(${a})`); }
      catch (e) {
        // the document was replaced mid-call (navigation): wait for the new one once
        if (i < 2 && /context|Cannot find|destroyed|navigat/i.test(e.message)) { await this.waitDoc(5000); continue; }
        throw e;
      }
    }
  }

  async url() { return this.eval("location.href").catch(() => ""); }

  async waitDoc(cap = 10_000) {
    const t0 = now();
    while (this.loading && now() - t0 < cap) await sleep(25);
  }

  // Event-driven settle: 2 animation frames; then, only if the action caused network or DOM
  // activity, wait until both are quiet for `quiet` ms (requests older than 2 s ignored).
  async settle({ since = now(), quiet = 150, cap = 2500, navCap = 10_000 } = {}) {
    const t0 = now();
    await this.waitDoc(navCap);
    try { await this.eval("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))", { timeout: 300 }); } catch {}
    for (;;) {
      const t = now();
      const net = [...this.inflight.values()].filter((s) => t - s < 2000).length;
      let ago = 1e9;
      try { ago = await this.eval("performance.now() - (window.__jevbMut ?? 0)", { timeout: 1000 }); } catch { await this.waitDoc(navCap); }
      const changed = ago < t - since + 5;
      if (net === 0 && (!changed || ago >= quiet)) break;
      if (now() - t0 > cap) break;
      if (this.loading) await this.waitDoc(navCap);
      await sleep(30);
    }
    return now() - t0;
  }

  // Wait for the page to change after snapshot `page` (its `at` stamp): the next DOM mutation,
  // a new document, or `cap` ms. Then settle. For pages that wait on a timer (spinners, delayed
  // content), where settle() alone returns at once because nothing is moving.
  // JS dialogs (alert, confirm, prompt) that opened at or after `since`, as one-line strings.
  dialogsSince(since) {
    return this.dialogs.filter((d) => d.at >= since).map((d) => `${d.type} "${d.message.slice(0, 120)}" ${d.accepted ? "accepted" : "dismissed"}`);
  }

  async waitChange(page, cap = 8000) {
    const t0 = now();
    let r = { changed: false };
    try { r = await this.J("waitChange", page?.at ?? null, cap); }
    catch { r = { changed: true }; await this.waitDoc(10_000); }   // navigated mid-wait
    if (this.loading || this.dialogAt >= t0) r.changed = true;
    await this.settle({ cap: 2000 });
    return { changed: !!r.changed, ms: now() - t0 };
  }

  async navigate(url) {
    this.events.length = 0;
    const since = now();
    this.loading = true;
    const r = await this.send("Page.navigate", { url }, 30_000);
    if (r.errorText) { this.loading = false; throw new Error(`navigation failed: ${r.errorText}`); }
    if (!r.loaderId) this.loading = false;   // same-document (hash) navigation
    await this.settle({ since, navCap: 20_000 });
  }

  async snapshot() {
    const s = await this.J("snapshot", { start: this.nextId });
    this.nextId = Math.max(this.nextId, s.next);
    return s;
  }

  async screenshot() {
    const { data } = await this.send("Page.captureScreenshot", { format: "jpeg", quality: 60 });
    return data;
  }

  // ---------------------------------------------------------------- input
  mouse(type, x, y, extra = {}) { return this.send("Input.dispatchMouseEvent", { type, x, y, button: "none", ...extra }); }
  async key(name, modifiers = 0) {
    const k = KEYS[name]; if (!k) throw new Error(`unknown key ${name}; known: ${KEY_NAMES.join(", ")}`);
    const base = { key: k.key ?? name, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk, modifiers };
    await this.send("Input.dispatchKeyEvent", { type: k.text ? "keyDown" : "rawKeyDown", ...base, ...(k.text ? { text: k.text, unmodifiedText: k.text } : {}) });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }

  // Resolve an id to a point. Closes a covering overlay (at most twice per URL) when a close
  // control is plain to see. Throws a short, model-readable error otherwise.
  async point(id, { allowDisabled = false } = {}) {
    let loc = await this.J("locate", id);
    if (!loc.ok) throw new Error(loc.reason === "gone" ? `element ${id} is gone (the page changed); observe again` : `element ${id} is ${loc.reason}`);
    if (loc.disabled && !allowDisabled) throw new Error(`element ${id} is disabled`);
    if (loc.covered && loc.closer) {
      const u = await this.url();
      this.dismissed ??= new Map();
      if ((this.dismissed.get(u) ?? 0) < 2) {
        this.dismissed.set(u, (this.dismissed.get(u) ?? 0) + 1);
        const c = await this.J("locate", loc.closer.id);
        if (c.ok && !c.covered) {
          await this.clickAt(c.x, c.y);
          this.events.push(`closed an overlay ("${loc.closer.text}") that covered the target`);
          await this.settle({ cap: 1000 });
          loc = await this.J("locate", id);
          if (!loc.ok) throw new Error(`element ${id} is ${loc.reason} after closing an overlay`);
        }
      }
    }
    if (loc.covered) throw new Error(`element ${id} is covered by ${loc.covered}`);
    return loc;
  }
  async clickAt(x, y, { button = "left", clickCount = 1 } = {}) {
    await this.mouse("mouseMoved", x, y);
    await this.mouse("mousePressed", x, y, { button, clickCount, buttons: button === "left" ? 1 : 2 });
    await this.mouse("mouseReleased", x, y, { button, clickCount });
  }

  // op: click | dblclick | right_click | hover | type | clear | press_enter | press_key | select |
  //     check | uncheck | drag | upload | scroll | wait | back | navigate
  // Returns a short note; throws a model-readable error. Never retries a mutation.
  async exec({ op, id, value, key, to, direction }) {
    switch (op) {
      case "click": case "dblclick": case "right_click": {
        const p = await this.point(id);
        await this.clickAt(p.x, p.y, { button: op === "right_click" ? "right" : "left", clickCount: op === "dblclick" ? 2 : 1 });
        if (op === "dblclick") await this.mouse("mousePressed", p.x, p.y, { button: "left", clickCount: 2 }).then(() => this.mouse("mouseReleased", p.x, p.y, { button: "left", clickCount: 2 }));
        return "";
      }
      case "check": case "uncheck": {
        const want = op === "check";
        const cur = await this.eval(`(() => { const e = window.__jevb?.el(${+id}); return e ? (e.checked ?? e.getAttribute("aria-checked") === "true") : null })()`);
        if (cur === want) return `already ${op}ed`;
        const p = await this.point(id);
        await this.clickAt(p.x, p.y);
        return "";
      }
      case "hover": { const p = await this.point(id, { allowDisabled: true }); await this.mouse("mouseMoved", p.x, p.y); return ""; }
      case "type": case "clear": {
        const v = op === "clear" ? "" : String(value ?? "");
        const p = await this.point(id);
        const special = /^(date|time|datetime-local|month|week|color|range)$/.test(p.type);
        if (!special) {
          await this.clickAt(p.x, p.y);
          await this.J("selectAll", id);
          if (!v) await this.key("Delete");
          else if (v.length <= 64 && !/[\n\r]/.test(v)) {
            // real key events per character: date pickers and input masks only listen to keys
            await this.key("Delete");
            for (const ch of v) {
              await this.send("Input.dispatchKeyEvent", { type: "keyDown", key: ch, text: ch, unmodifiedText: ch });
              await this.send("Input.dispatchKeyEvent", { type: "keyUp", key: ch });
            }
          } else await this.send("Input.insertText", { text: v });
        }
        let got = await this.J("getValue", id).catch(() => null);
        if (got !== null && got !== v && !(p.editable && got.trim() === v.trim())) got = await this.J("setValue", id, v);
        return special ? "set" : "";
      }
      case "press_enter": {
        if (id != null) { const p = await this.point(id); if (!(await this.J("focus", id))) await this.clickAt(p.x, p.y); }
        await this.key("Enter"); return "";
      }
      case "press_key": {
        if (id != null) { await this.point(id, { allowDisabled: true }); await this.J("focus", id); }
        await this.key(key || value || "Escape"); return "";
      }
      case "select": {
        await this.point(id);
        const r = await this.J("selectOption", id, value);
        if (r.ok) return `chose "${r.chosen}"`;
        if (r.error === "not a native select") {   // custom dropdown: open it; the caller picks the option next
          const p = await this.point(id); await this.clickAt(p.x, p.y); return "opened a custom dropdown; pick the option next";
        }
        throw new Error(`${r.error}${r.options ? `; options: ${r.options.join(", ")}` : ""}`);
      }
      case "drag": {
        if (to == null) throw new Error("drag needs `to` (the element to drop onto)");
        const a = await this.point(id), b = await this.point(to, { allowDisabled: true });
        const html5 = await this.eval(`(() => { const e = window.__jevb?.el(${+id}); return !!e && (e.draggable || e.getAttribute("draggable") === "true") })()`);
        if (html5) { await this.J("drag", id, to); return "html5 drag"; }
        await this.mouse("mouseMoved", a.x, a.y);
        await this.mouse("mousePressed", a.x, a.y, { button: "left", clickCount: 1, buttons: 1 });
        for (let i = 1; i <= 8; i++) await this.mouse("mouseMoved", a.x + ((b.x - a.x) * i) / 8, a.y + ((b.y - a.y) * i) / 8, { buttons: 1 });
        await this.mouse("mouseReleased", b.x, b.y, { button: "left", clickCount: 1 });
        return "";
      }
      case "upload": {
        if (!value) throw new Error("upload needs a file path value");
        const { result } = await this.send("Runtime.evaluate", { expression: `window.__jevb?.el(${+id})` });
        if (!result?.objectId) throw new Error(`element ${id} is gone; observe again`);
        await this.send("DOM.setFileInputFiles", { files: String(value).split("\n"), objectId: result.objectId });
        await this.eval(`(() => { const e = window.__jevb.el(${+id}); e.dispatchEvent(new Event("input", {bubbles:true})); e.dispatchEvent(new Event("change", {bubbles:true})); })()`).catch(() => {});
        return "";
      }
      case "scroll": {
        const aim = await this.J("scrollAim", id ?? null);
        const before = await this.J("scrollSum");
        const step = Math.max(100, Math.round(aim.h * 0.8));
        await this.mouse("mouseWheel", aim.x, aim.y, { deltaX: 0, deltaY: (direction || value) === "up" ? -step : step });
        let last = before;
        for (const t0 = now(); now() - t0 < 1000;) {
          await sleep(40);
          const s = await this.J("scrollSum");
          if (s === last && (s !== before || now() - t0 >= 300)) break;
          last = s;
        }
        return last === before ? "nothing moved (at the end, or nothing scrollable there)" : `scrolled ${Math.round(last - before)}px`;
      }
      case "wait": {   // a number: sleep that many ms; otherwise until the page next changes (max 8 s)
        if (Number(value)) { await sleep(Math.min(Number(value), 10_000)); return ""; }
        const w = await this.waitChange(null, 8000);
        return w.changed ? `page changed after ${Math.round(w.ms)} ms` : `no change in ${Math.round(w.ms / 1000)} s`;
      }
      case "back": this.loading = true; await this.eval("history.back()").catch(() => {}); await sleep(50); if (!this.inflight.size) this.loading = false; return "";
      case "navigate": await this.navigate(String(value)); return "";
      default: throw new Error(`unknown op "${op}"`);
    }
  }
}
