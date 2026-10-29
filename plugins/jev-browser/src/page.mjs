// Code that runs inside the page, in one Runtime.evaluate per call. INSTALL builds window.__jevb
// once per document; every later call reuses it. Self-contained: it is sent as source text.
//
// Element ids are sticky: a node keeps its id while its role and name stay the same
// (Playwright's rule), so ids survive re-snapshots and diffs make sense. Ids are never reused
// within a session (the daemon passes the next free number to each new document), so an id
// from a page that navigated away fails as "gone" instead of hitting another element.
export const VERSION = 11;

// COLLECTS is safety.mjs formCollects (passed in as source): what contact or payment details a form asks for.
export const INSTALL = function (VERSION, COLLECTS) {
  if (window.__jevb && window.__jevb.v === VERSION) return window.__jevb;
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], [role=tab], [role=checkbox], [role=radio], [role=switch], [role=option], [role=combobox], [role=textbox], [role=searchbox], [role=slider], [role=spinbutton], [role=treeitem], [contenteditable=""], [contenteditable=true], [onclick], [oncontextmenu], [ondblclick], [draggable=true], [tabindex]:not([tabindex="-1"])';
  const clean = (s, n = 80) => String(s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const css = (el) => el.ownerDocument.defaultView.getComputedStyle(el);
  const J = { v: VERSION, els: new Map(), ids: new WeakMap(), next: 1, prev: new Set() };

  // Offset of el's frame in top-level viewport coordinates (same-origin frames only).
  const frameOffset = (el) => {
    let x = 0, y = 0, w = el.ownerDocument.defaultView;
    while (w && w !== window && w.frameElement) {
      const f = w.frameElement, r = f.getBoundingClientRect();
      x += r.left + f.clientLeft; y += r.top + f.clientTop; w = f.ownerDocument.defaultView;
    }
    return { x, y };
  };
  // The deepest element at a top-level point, through open shadow roots and same-origin frames.
  const deepHit = (x, y) => {
    let n = document.elementFromPoint(x, y), ox = 0, oy = 0;
    for (let k = 0; n && k < 12; k++) {
      if (n.shadowRoot) { const m = n.shadowRoot.elementFromPoint(x - ox, y - oy); if (m && m !== n) { n = m; continue; } }
      if (n.tagName === "IFRAME" || n.tagName === "FRAME") {
        let d = null; try { d = n.contentDocument; } catch {}
        if (d) { const r = n.getBoundingClientRect(); ox += r.left + n.clientLeft; oy += r.top + n.clientTop; const m = d.elementFromPoint(x - ox, y - oy); if (m) { n = m; continue; } }
      }
      break;
    }
    return n;
  };
  // contains() across shadow boundaries
  const within = (outer, n) => { for (; n; n = n.parentNode || n.host) if (n === outer) return true; return false; };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = css(el);
    return st.visibility !== "hidden" && st.display !== "none" && (+st.opacity > 0.05 || /^(checkbox|radio|file)$/.test(el.type));
  };
  const byIds = (el, ids) => clean((ids || "").split(/\s+/).map((id) => el.ownerDocument.getElementById(id)?.innerText).filter(Boolean).join(" "));
  const labelOf = (el) => clean(byIds(el, el.getAttribute("aria-labelledby")) || [...(el.labels || [])].map((l) => l.innerText).join(" ") || el.getAttribute("aria-label") || "");
  const sibText = (el) => {
    let t = "";
    for (let n = el.nextSibling; n && t.trim().length < 40; n = n.nextSibling) {
      if (n.nodeType === 1 && n.matches("input, select, textarea, button, br, label, div, p, li")) break;
      t += n.textContent;
    }
    return clean(t, 60);
  };
  const defaultButton = (f) => f && [...f.elements].find((x) => (x.tagName === "BUTTON" && (x.type || "submit") === "submit") || (x.tagName === "INPUT" && /^(submit|image)$/.test(x.type)));
  const submitterOf = (el) => {
    if (!el || !/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || /^(submit|image|button|reset)$/.test(el.type)) return "";
    const b = defaultButton(el.form || el.closest("form"));
    return b ? clean(b.tagName === "INPUT" ? (b.value || b.alt || b.getAttribute("aria-label")) : (b.innerText || b.getAttribute("aria-label") || b.title), 60) : "";
  };
  // per snapshot: the kinds of contact or payment details each form collects ("" for none)
  let formCache = new Map();
  const collectsOf = (f) => {
    if (!f || !COLLECTS) return "";
    if (!formCache.has(f)) formCache.set(f, COLLECTS([...f.elements].map((x) => ({ type: x.tagName === "INPUT" ? (x.getAttribute("type") || "text") : x.tagName.toLowerCase(), name: x.name, id: x.id, autocomplete: x.getAttribute("autocomplete"), label: labelOf(x), placeholder: x.getAttribute("placeholder") }))));
    return formCache.get(f);
  };
  const formOf = (el) => el.form || el.closest?.("form");
  const isSubmit = (el) => (el.tagName === "BUTTON" && (el.getAttribute("type") || "submit").toLowerCase() === "submit") || (el.tagName === "INPUT" && /^(submit|image)$/i.test(el.type));
  const cls = (n) => String(n?.className?.baseVal ?? n?.className ?? "");
  const FILLER = /^(selected|active|open|checked|disabled|hover|focus|show|hide|hidden|fade|flex|grid|block|inline|items|justify|center|rounded|text|font|medium|bold|icon|btn|absolute|relative|fixed|sticky|static|top|bottom|left|right|full|auto|none|border|shadow|ring|offset|background|transition|colors|duration|ease|cursor|pointer|outline|group|container|wrapper|inner|outer|root|item|link|only|content|overflow|truncate|nowrap|gap|space|default|base|mui|css|style|component|wrap)$/i;
  const hintOf = (el) => {
    const words = [el.id, cls(el), cls(el.firstElementChild)].join(" ").split(/[\s_\-:/.]+/).flatMap((t) => {
      const parts = t.replace(/([a-z])([A-Z])/g, "$1 $2").split(" ");
      return parts.length > 1 && parts.some((x) => x.length <= 2) ? [] : parts;
    }).filter((w) => w && w.length > 2 && !/\d/.test(w) && !/^[b-df-hj-np-tv-xz]+$/i.test(w) && !FILLER.test(w));
    return [...new Set(words.map((w) => w.toLowerCase()))].slice(0, 4).join(" ");
  };

  // Every element in DOM order, through open shadow roots and same-origin frames.
  const collect = () => {
    const all = []; let xframes = 0, fc = 0;
    const walk = (root, frame) => {
      for (const el of root.querySelectorAll("*")) {
        all.push([el, frame]);
        if (el.shadowRoot) walk(el.shadowRoot, frame);
        if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
          let d = null; try { d = el.contentDocument; } catch {}
          if (d && d.documentElement) walk(d, ++fc);
          else if (el.src && !/^about:|^javascript:/.test(el.src)) xframes++;
        }
      }
    };
    walk(document, 0);
    return { all, xframes };
  };

  // Text a sighted user cannot see, though it has a box: invisible or transparent (closed
  // mega-menus), or clipped screen-reader-only text (.sr-only, .visually-hidden). Cached per
  // element for one snapshot. checkVisibility() already covers ancestors' opacity and
  // visibility, so it runs once, on the nearest ancestor with a box (a `display: contents`
  // wrapper has none and always fails it: GitHub's PR list sits in several). The clip test
  // walks up, since .sr-only clips a container, not the text's own parent.
  let hidCache = new Map(), srCache = new Map();
  const srOnly = (el) => {
    if (!el || el.nodeType !== 1 || el === document.body) return false;
    if (srCache.has(el)) return srCache.get(el);
    const st = css(el);
    let h = false;
    if ((st.clip !== "auto" && st.position === "absolute") || /inset\(50%\)|inset\(100%\)/.test(st.clipPath)) {
      const r = el.getBoundingClientRect(); h = r.width <= 2 || r.height <= 2;
    } else if (st.overflow === "hidden" && st.position === "absolute") {
      const r = el.getBoundingClientRect(); h = r.width <= 1 || r.height <= 1;
    }
    if (!h) h = srOnly(el.parentElement);
    srCache.set(el, h);
    return h;
  };
  const hiddenText = (el) => {
    if (!el || el.nodeType !== 1 || el === document.body) return false;
    if (hidCache.has(el)) return hidCache.get(el);
    let box = el;
    while (box && box !== document.body && css(box).display === "contents") box = box.parentElement;
    const h = !!(box && box !== document.body && box.checkVisibility && !box.checkVisibility({ opacityProperty: true, visibilityProperty: true })) || srOnly(el);
    hidCache.set(el, h);
    return h;
  };

  J.snapshot = ({ start = 1, maxEls = 4000 } = {}) => {
    hidCache = new Map(); srCache = new Map(); formCache = new Map();
    if (J.next < start) J.next = start;
    const { all, xframes } = collect();
    const out = [], boxes = {}, seen = new Set(), now = new Set(), W = innerWidth, H = innerHeight;
    for (const [el, frame] of all) {
      const tag = el.tagName.toLowerCase();
      let pick = el.matches(SEL);
      if (!pick && !["html", "body", "label", "svg", "path", "iframe"].includes(tag) && el.parentElement) {
        // JS-bound clickables usually show a pointer cursor; the parent check keeps only the outermost
        const cur = css(el).cursor;
        pick = cur === "pointer" && css(el.parentElement).cursor !== "pointer" && !el.parentElement.closest("a, button, [role=button]");
      }
      if (!pick && ((tag === "th" && el.closest("thead")) || (tag === "img" && !el.closest("a, button")))) {
        const r = el.getBoundingClientRect(); pick = tag === "th" || (r.width >= 24 && r.height >= 24);
      }
      if (!pick || el.closest('[aria-hidden="true"], [inert]')) continue;
      const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : null;
      let hit = el, hidden = false;
      if (!visible(el)) {
        const lab = (type === "checkbox" || type === "radio") && [...(el.labels || [])].find(visible);
        if (lab) hit = lab; else if (type === "file") hidden = true; else continue;
      }
      if (hit === el && (type === "checkbox" || type === "radio")) {
        const st = css(el), r = el.getBoundingClientRect();
        if (+st.opacity <= 0.05 || r.width <= 2 || r.height <= 2 || st.clip !== "auto" || st.clipPath !== "none") {
          const lab = [...(el.labels || [])].find(visible); if (lab) hit = lab;
        }
      }
      if (seen.has(hit)) continue; seen.add(hit);

      const role = el.getAttribute("role");
      const o = {};
      o.tag = type ? `input:${type}` : (role && !["a", "button", "select", "textarea"].includes(tag) ? `${tag}[${role}]` : tag);
      if (frame) o.frame = frame;
      if (hidden) o.hidden = true;
      const isField = ["input", "select", "textarea"].includes(tag) || ["textbox", "searchbox", "combobox", "spinbutton"].includes(role) || el.isContentEditable;
      if (isField) {
        const label = labelOf(el); if (label) o.label = label;
        const ph = el.getAttribute("placeholder"); if (ph) o.placeholder = clean(ph, 60);
        if (!label && !ph && el.name) o.name = el.name;
        // the button Enter in this field presses (its form's default button): the irreversible
        // gate judges press_enter by it, since the field's own label says nothing about what commits
        const sub = submitterOf(el); if (sub) o.submitter = sub;
        if (tag === "select") {
          o.value = clean(el.selectedOptions?.[0]?.text, 40);
          o.options = [...el.options].slice(0, 40).map((op) => clean(op.text, 30));
        } else if (type === "checkbox" || type === "radio") {
          o.checked = el.checked;
          if (!o.label && hit !== el) o.label = clean(hit.innerText);
          if (!o.label) { const t = sibText(el); if (t) o.label = t; }
          if (el.value && el.value !== "on") o.value = clean(el.value, 30);
        } else if (["submit", "button", "reset"].includes(type)) {
          o.text = clean(el.value || label);
        } else if (type === "password") {
          if (el.value) o.value = "[filled]";
        } else if (type !== "file") {
          const v = el.isContentEditable && tag !== "input" ? el.innerText : el.value;
          if (v) o.value = clean(v, 60);
        }
      } else {
        const img = el.querySelector("img[alt], svg title");
        const text = (tag === "img" ? clean(el.getAttribute("alt")) : el.textContent.length > 1500 ? clean(el.textContent) : clean(el.innerText)) || labelOf(el) || clean(el.getAttribute("title")) || clean(img?.getAttribute?.("alt") || img?.textContent);
        if (text) o.text = text;
        const label = labelOf(el); if (label && label !== text) o.label = label;
        if (!text && tag !== "img") { const h = hintOf(el); if (h) o.hint = h; }
      }
      const href = el.getAttribute("href");
      if (href && !href.startsWith("javascript")) {
        try { const u = new URL(href, el.ownerDocument.location.href); o.href = clean(u.origin === location.origin ? u.pathname + u.search + u.hash : u.href, 80); } catch { o.href = clean(href, 80); }
      }
      // a submit button, and what its form (or a field's form) collects: the gate pauses on
      // submitting contact or payment details whatever the button says
      const form = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(el.tagName) ? formOf(el) : null;
      if (form && isSubmit(el)) o.submits = true;
      const col = form ? collectsOf(form) : ""; if (col) o.collects = col;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") o.disabled = true;
      if (el.getAttribute("aria-busy") === "true") o.busy = true;
      const srt = el.getAttribute("aria-sort");   // class names (headerSortDown…) mean opposite things on different sites
      if (srt && srt !== "none") o.sorted = /asc|up/i.test(srt) ? "ascending" : /desc|down/i.test(srt) ? "descending" : srt;
      if (el.hasAttribute("aria-expanded")) o.expanded = el.getAttribute("aria-expanded") === "true";
      if (el.getAttribute("aria-checked")) o.checked = el.getAttribute("aria-checked") === "true";
      if (el.getAttribute("aria-selected") === "true" || el.getAttribute("aria-current") || el.getAttribute("aria-pressed") === "true" || /\b(selected|active)\b/.test(cls(el))) o.active = true;
      const own = o.text || o.label || o.placeholder || "";
      if (own.length < 16 || isField) {
        let p = hit.parentElement;
        for (let k = 0; p && k < 5; k++, p = p.parentElement) {
          if (p.textContent.length > 600) break;   // innerText of a big ancestor costs a full layout walk
          const t = clean(p.innerText, 400);
          if (t && t !== own) { if (t.length <= 100) o.near = t; break; }
        }
        const row = hit.closest("li, tr, [role=row], [role=listitem]");
        const m = row && cls(row).match(/\b(completed|done|selected|active|checked|disabled|error|expanded)\b/i);
        if (m) o.row_state = m[1];
      }
      // geometry in top-level coordinates
      const off = frameOffset(hit), rr = hit.getBoundingClientRect();
      const cx = off.x + rr.left + rr.width / 2, cy = off.y + rr.top + rr.height / 2;
      const inView = cx >= 0 && cy >= 0 && cx < W && cy < H;
      if (!hidden && !inView) o.off = cy < 0 ? "above" : cy >= H ? "below" : "side";
      if (!hidden && inView) {
        const top = deepHit(cx, cy);
        if (top && top !== hit && !within(hit, top) && !within(top, hit) && !(top.tagName === "LABEL" && top.control === el)) o.covered = true;
      }
      // sticky id: kept while role + name stay the same
      const sig = `${o.tag}|${(o.label || o.text || o.placeholder || o.name || o.hint || "").slice(0, 40)}`;
      const known = J.ids.get(el);
      let id;
      if (known && known.sig === sig) id = known.id;
      else { id = J.next++; J.ids.set(el, { id, sig }); }
      J.els.set(id, { el, hit, sig });
      if (!J.prev.has(id) && J.prev.size) o.new = true;
      now.add(id);
      out.push({ id, ...o });
      // viewport box (top-level px) for the image fallback: lets Jev tie what it sees to an `i`
      if (!hidden && inView) boxes[id] = [Math.round(off.x + rr.left), Math.round(off.y + rr.top), Math.round(rr.width), Math.round(rr.height)];
      if (out.length >= maxEls) break;
    }
    for (const [id, v] of J.els) if (!v.el.isConnected) J.els.delete(id);
    J.prev = now;

    const dialogs = [...document.querySelectorAll('dialog[open], [role=dialog], [role=alertdialog], [aria-modal="true"]')].filter(visible).map((d) => clean(d.innerText, 400)).filter(Boolean);
    for (let n = document.elementFromPoint(W / 2, H / 2); n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const st = getComputedStyle(n), r = n.getBoundingClientRect();
      if ((st.position === "fixed" || st.position === "sticky") && r.width * r.height >= 0.6 * W * H) {
        const t = clean(n.innerText, 400); if (t && !dialogs.includes(t)) dialogs.push(t); break;
      }
    }
    let text = "";
    if (document.body) {
      const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), range = document.createRange();
      for (let n = tw.nextNode(); n && text.length < 2500; n = tw.nextNode()) {
        if (!n.textContent.trim() || !n.parentElement || ["SCRIPT", "STYLE", "NOSCRIPT"].includes(n.parentElement.tagName)) continue;
        range.selectNodeContents(n); const r = range.getBoundingClientRect();
        if (r.bottom < 0 || r.top > H || r.width === 0) continue;
        if (hiddenText(n.parentElement)) continue;
        text += " " + n.textContent;
      }
      text = clean(text, 2500);
    }
    // Visible data tables, summarised so row order and sort state are plain to read (flat page
    // text hides both): headers marked with aria-sort, or with the order their cells are in.
    const tables = [];
    for (const t of document.querySelectorAll('table, [role=grid], [role=table]')) {
      if (tables.length >= 4) break;
      const r = t.getBoundingClientRect();
      if (!r.width || r.bottom < 0 || r.top > H || !visible(t)) continue;
      const rows = [...t.querySelectorAll('tr, [role=row]')].filter((x) => x.closest('table, [role=grid], [role=table]') === t).slice(0, 60);
      const cells = (row) => [...row.querySelectorAll(':scope > th, :scope > td, :scope > [role=columnheader], :scope > [role=cell], :scope > [role=gridcell], :scope > [role=rowheader]')];
      const head = rows.find((x) => cells(x).some((c) => c.tagName === "TH" || c.getAttribute("role") === "columnheader"));
      const body = rows.filter((x) => x !== head && cells(x).length && !cells(x).every((c) => c.tagName === "TH"));
      if (!head || body.length < 2) continue;
      const hs = cells(head);
      const num = (s) => { const m = s.replace(/[$€£,\s%]/g, ""); return /^-?\d+(\.\d+)?$/.test(m) ? +m : null; };
      const columns = hs.slice(0, 12).map((h, k) => {
        const name = clean(h.innerText, 30) || `col ${k + 1}`;
        const aria = h.getAttribute("aria-sort") || h.closest("[aria-sort]")?.getAttribute("aria-sort");
        if (aria && aria !== "none") return `${name} (sorted ${aria})`;
        const vals = body.map((x) => clean(cells(x)[k]?.innerText ?? "", 40)).filter(Boolean);
        if (vals.length < 3 || new Set(vals).size < 3) return name;
        const ns = vals.map(num), numeric = ns.every((v) => v != null);
        const cmp = (a, b, i) => (numeric ? ns[i] - ns[i + 1] : a.localeCompare(b, undefined, { sensitivity: "base", numeric: true }));
        let asc = true, desc = true;
        for (let i = 0; i < vals.length - 1; i++) { const c = cmp(vals[i], vals[i + 1], i); if (c > 0) asc = false; if (c < 0) desc = false; }
        return asc ? `${name} (rows in ascending order)` : desc ? `${name} (rows in descending order)` : name;
      });
      const first = body.slice(0, 6).map((x) => clean(cells(x)[0]?.innerText ?? "", 30));
      tables.push({ table: tables.length + 1, columns, rows: body.length, first_column: first });
    }
    const full = document.body?.innerText ?? "";
    const captcha = [...document.querySelectorAll("iframe[src]")].some((f) => /recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha/i.test(f.src))
      || /verify you are (a )?human|are you a robot|press (and|&) hold|unusual traffic from your computer|checking your browser/i.test(full.slice(0, 5000));
    return {
      url: location.href, title: document.title, text, at: { origin: performance.timeOrigin, t: performance.now() },
      metrics: { scroll_y: Math.round(scrollY), page_height: document.documentElement.scrollHeight, viewport_h: H, text_length: full.length, elements: out.length, cross_origin_frames: xframes || undefined },
      dialogs, captcha, tables: tables.length ? tables : undefined, elements: out, boxes, next: J.next,
    };
  };

  // Resolve an id to a fresh point to act on, after scrolling it into view.
  // Fails safely: gone (removed or another page), changed (role/name differ), hidden, covered.
  J.locate = (id, { scroll = true } = {}) => {
    const rec = J.els.get(id);
    if (!rec || !rec.el.isConnected) return { ok: false, reason: "gone" };
    const { el, hit } = rec;
    if (!hit.isConnected) return { ok: false, reason: "gone" };
    const W = innerWidth, H = innerHeight;
    let off = frameOffset(hit), r = hit.getBoundingClientRect();
    let cx = off.x + r.left + r.width / 2, cy = off.y + r.top + r.height / 2;
    if (scroll && (cx < 0 || cy < 0 || cx >= W || cy >= H || r.width < 1)) {
      hit.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      off = frameOffset(hit); r = hit.getBoundingClientRect();
      cx = off.x + r.left + r.width / 2; cy = off.y + r.top + r.height / 2;
    }
    const fileish = el.tagName === "INPUT" && el.type === "file";
    if ((r.width < 1 || r.height < 1) && !fileish) return { ok: false, reason: "hidden" };
    const disabled = !!(el.disabled || el.getAttribute("aria-disabled") === "true");
    const res = { ok: true, x: cx, y: cy, disabled, tag: el.tagName.toLowerCase(), type: el.type || "", editable: el.isContentEditable };
    if (cx < 0 || cy < 0 || cx >= W || cy >= H) { res.offscreen = true; return res; }
    const top = deepHit(cx, cy);
    if (top && top !== hit && !within(hit, top) && !within(top, hit) && !(top.tagName === "LABEL" && top.control === el) && !fileish) {
      res.covered = clean(`${top.tagName.toLowerCase()} ${top.innerText || top.getAttribute("aria-label") || ""}`, 60);
      res.closer = J.closer(top);
    }
    return res;
  };

  // A control that plainly closes the overlay that covers a target: walk up from the covering
  // element to its outermost positioned layer and look for close / dismiss / accept buttons.
  const CLOSE = /^\s*(close|dismiss|got it|no,? thanks|not now|accept( all)?( cookies)?|i agree|agree|allow all|ok|continue|[×✕✖xX])\s*$/i;
  J.closer = (top) => {
    let layer = top;
    for (let n = top; n && n.nodeType === 1; n = n.parentElement || n.getRootNode?.().host) {
      const st = css(n);
      if (st.position === "fixed" || st.position === "absolute" || n.matches?.('dialog, [role=dialog], [aria-modal="true"]')) layer = n;
    }
    for (const c of [...layer.querySelectorAll("button, a, [role=button], [aria-label], [class*=close], [id*=close]")].reverse()) {
      const t = clean(c.innerText || c.value || c.getAttribute("aria-label") || c.getAttribute("title"), 40);
      if ((t && CLOSE.test(t)) || (!t && /close|dismiss/i.test(c.id + " " + cls(c)))) {
        if (!visible(c)) continue;
        const known = J.ids.get(c); const id = known ? known.id : J.next++;
        if (!known) J.ids.set(c, { id, sig: `closer|${t}` });
        J.els.set(id, { el: c, hit: c, sig: known?.sig || `closer|${t}` });
        return { id, text: t || "close" };
      }
    }
    return null;
  };

  const target = (id) => { const r = J.els.get(id); if (!r || !r.el.isConnected) throw new Error("gone"); return r.el; };
  const fire = (el, ...types) => { for (const t of types) el.dispatchEvent(new Event(t, { bubbles: true })); };
  J.el = target;
  // document.activeElement (through shadow roots and same-origin frames) as a gate brief
  J.focused = () => {
    let a = document.activeElement;
    for (let k = 0; a && k < 8; k++) {
      if (a.shadowRoot?.activeElement) { a = a.shadowRoot.activeElement; continue; }
      if (/^I?FRAME$/.test(a.tagName)) { let d = null; try { d = a.contentDocument; } catch {} if (d?.activeElement) { a = d.activeElement; continue; } }
      break;
    }
    if (!a || a === document.body || a === document.documentElement) return null;
    const tag = a.tagName.toLowerCase(), type = tag === "input" ? (a.getAttribute("type") || "text").toLowerCase() : null;
    const f = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(a.tagName) ? formOf(a) : null;
    formCache = new Map();
    return { tag: type ? `input:${type}` : tag, text: clean(type ? (/^(submit|button|reset|image)$/.test(type) ? a.value || a.alt : "") : a.innerText), label: labelOf(a), submitter: submitterOf(a) || undefined, submits: (f && isSubmit(a)) || undefined, collects: (f && collectsOf(f)) || undefined };
  };
  J.focus = (id) => { const el = target(id); el.focus({ preventScroll: true }); return el.ownerDocument.activeElement === el || el.contains(el.ownerDocument.activeElement); };
  J.selectAll = (id) => {
    const el = target(id);
    el.focus({ preventScroll: true });
    if (typeof el.select === "function" && /^(INPUT|TEXTAREA)$/.test(el.tagName)) { try { el.select(); } catch {} return true; }
    if (el.isContentEditable) { const s = el.ownerDocument.getSelection(), r = el.ownerDocument.createRange(); r.selectNodeContents(el); s.removeAllRanges(); s.addRange(r); return true; }
    return false;
  };
  J.getValue = (id) => { const el = target(id); return el.isContentEditable && el.tagName !== "INPUT" ? el.innerText : el.value; };
  // Native setter + events: works with React/Vue controlled inputs and date/color/range inputs.
  J.setValue = (id, v) => {
    const el = target(id);
    if (el.isContentEditable && el.tagName !== "INPUT") { el.innerText = v; fire(el, "input", "change"); return el.innerText; }
    let val = v;
    if (el.type === "date") { const m = String(v).match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/); if (m) val = `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}`; }
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const set = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    set ? set.call(el, val) : (el.value = val);
    fire(el, "input", "change");
    return el.value;
  };
  J.selectOption = (id, want) => {
    const el = target(id);
    if (el.tagName !== "SELECT") return { ok: false, error: "not a native select" };
    const w = String(want).trim().toLowerCase(), opts = [...el.options];
    const op = opts.find((o) => o.text.trim().toLowerCase() === w) || opts.find((o) => o.value.toLowerCase() === w)
      || opts.find((o) => o.text.trim().toLowerCase().includes(w));
    if (!op) return { ok: false, error: `no option "${want}"`, options: opts.slice(0, 30).map((o) => o.text.trim()) };
    if (op.disabled) return { ok: false, error: `option "${op.text.trim()}" is disabled` };
    el.focus({ preventScroll: true });
    op.selected = true; el.value = op.value;
    fire(el, "input", "change");
    return { ok: true, chosen: op.text.trim() };
  };
  // HTML5 drag and drop with a shared DataTransfer (CDP mouse events do not start a native drag).
  J.drag = (a, b) => {
    const src = target(a), dst = target(b), dt = new DataTransfer();
    const at = (el) => { const r = el.getBoundingClientRect(); return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 }; };
    const ev = (el, type) => el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt, ...at(el) }));
    ev(src, "dragstart"); ev(dst, "dragenter"); ev(dst, "dragover"); ev(dst, "drop"); ev(src, "dragend");
    return true;
  };
  // Where to aim the wheel: the box around an element, or the largest visible scrollable area.
  J.scrollAim = (id) => {
    const scrolls = (el) => /(auto|scroll|overlay)/.test(css(el).overflowY) && el.scrollHeight > el.clientHeight + 1;
    const box = (r) => { const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0), h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0); return w > 0 && h > 0 ? { h, x: Math.max(r.left, 0) + w / 2, y: Math.max(r.top, 0) + h / 2, area: w * h } : null; };
    if (id != null) {
      for (let n = target(id); n && n !== document.body && n !== document.documentElement; n = n.parentElement) if (scrolls(n)) { const b = box(n.getBoundingClientRect()); if (b) return b; }
    }
    const doc = document.scrollingElement;
    if (doc && doc.scrollHeight > innerHeight + 1 && getComputedStyle(document.body).overflowY !== "hidden") return { h: innerHeight, x: innerWidth / 2, y: innerHeight / 2 };
    let best = null;
    for (const el of document.querySelectorAll("body *")) { if (!scrolls(el)) continue; const b = box(el.getBoundingClientRect()); if (b && (!best || b.area > best.area)) best = b; }
    return best || { h: innerHeight, x: innerWidth / 2, y: innerHeight / 2 };
  };
  J.scrollSum = () => { let s = scrollY; for (const el of document.querySelectorAll("body *")) if (el.scrollTop) s += el.scrollTop; return s; };
  // Draw each element's [i] mark over the page for one screenshot (pointer-events: none, in a
  // closed shadow root on <html>, so hit tests and the page's own body observers never see it).
  // The jevb mutation stamp is not moved: the marks are not a page change.
  J.marks = (boxes) => {
    const obs = window.__jevbMutObs, keep = window.__jevbMut;
    J.unmark();
    const host = document.createElement("jevb-marks"); host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    const root = host.attachShadow({ mode: "closed" });
    let html = "";
    for (const [id, [x, y, w, h]] of Object.entries(boxes || {})) {
      html += `<div style="position:fixed;left:${x}px;top:${y}px;width:${w}px;height:${h}px;outline:2px solid #e0115f;box-sizing:border-box"></div>`
        + `<div style="position:fixed;left:${Math.max(0, x)}px;top:${Math.max(0, y - 14)}px;font:bold 11px/13px monospace;background:#e0115f;color:#fff;padding:0 2px">${+id}</div>`;
    }
    root.innerHTML = html;
    document.documentElement.appendChild(host); J.markHost = host;
    obs?.takeRecords(); window.__jevbMut = keep;
    return Object.keys(boxes || {}).length;
  };
  J.unmark = () => {
    const obs = window.__jevbMutObs, keep = window.__jevbMut;
    J.markHost?.remove(); J.markHost = null;
    obs?.takeRecords(); window.__jevbMut = keep;
    return true;
  };
  J.readText = (max = 12000) => clean(document.body?.innerText ?? "", max);
  J.mutated = () => performance.now() - (window.__jevbMut ?? 0);
  // Resolve on the first DOM change after `since` (a snapshot's `at`), or after `cap` ms.
  // A different document (navigation) counts as a change. Event-driven: a MutationObserver,
  // not polling, so a page that waits on a JS timer is picked up the moment it renders.
  J.waitChange = (since, cap = 8000) => new Promise((res) => {
    const t0 = performance.now();
    const fin = (changed) => { mo.disconnect(); clearTimeout(timer); res({ changed, waited: Math.round(performance.now() - t0) }); };
    const mo = new MutationObserver(() => fin(true));
    let timer;
    if (since && (since.origin !== performance.timeOrigin || (window.__jevbMut ?? 0) > since.t)) return res({ changed: true, waited: 0 });
    mo.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    timer = setTimeout(() => fin(false), cap);
  });
  return (window.__jevb = J);
};

// Installed on every new document: remembers when the DOM last changed, for the settler.
export const MUTATION_WATCH = `(() => {
  if (window.__jevbMutObs) return; window.__jevbMut = performance.now();
  const mo = new MutationObserver(() => { window.__jevbMut = performance.now(); });
  window.__jevbMutObs = mo;
  const go = () => mo.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  document.documentElement ? go() : addEventListener("DOMContentLoaded", go);
})()`;
