// Live regression tests for how `do` ends: every ending goes through the `complete` confirm
// question. They drive the real daemon, Chrome and Jev, so they run only with JEVB_LIVE=1:
//   JEVB_LIVE=1 node --test tests/do-stop.live.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const JEVB = fileURLToPath(new URL("../bin/jevb", import.meta.url));
const live = { skip: process.env.JEVB_LIVE !== "1" && "set JEVB_LIVE=1 (needs network, Chrome and the Jev key)" };
const run = (session, ...args) => execFileSync(JEVB, ["--session", session, ...args], { encoding: "utf8", timeout: 120_000 });
const doJson = (session, goal) => { const r = JSON.parse(run(session, "do", goal, "--json")); return r.data ?? r; };
const evalJs = (session, js) => JSON.parse(run(session, "eval", js));

test("ti-add-remove: not done at 2 Delete buttons, done at exactly 3", live, () => {
  const s = "test-add-remove";
  try {
    run(s, "open", "https://the-internet.herokuapp.com/add_remove_elements/");
    const d = doJson(s, "Add elements until there are exactly 3 Delete buttons");
    assert.equal(evalJs(s, "() => document.querySelectorAll('.added-manually').length"), 3);
    assert.equal(d.status, "done");
    // no round may end as done while fewer than 3 buttons exist: each confirm before the last is low
    for (const r of d.rounds.slice(0, -1)) if (r.confirm != null) assert.ok(r.confirm < 0.65, `round ${r.round} confirm ${r.confirm}`);
  } finally { run(s, "close"); }
});

test("ti-sort-table: done after one header click, not toggled back", live, () => {
  const s = "test-sort-table";
  try {
    run(s, "open", "https://the-internet.herokuapp.com/tables");
    const d = doJson(s, "Sort the first table by Last Name");
    assert.equal(d.status, "done");
    // the last round decides to stop, so only earlier rounds acted
    assert.equal(d.rounds.slice(0, -1).filter((r) => r.tool === "click").length, 1);
    assert.equal(evalJs(s, "() => document.querySelector('#table1 tbody tr td').innerText"), "Bach");
  } finally { run(s, "close"); }
});

// `wait` blocks until the page changes (not a fixed sleep counted in rounds): the result appears
// on a 5 s JS timer with no network or DOM activity before it, and must be seen as soon as it renders.
test("ti-dynamic-loading: done or likely_done in under 8 s, 5 of 5 runs", live, () => {
  const s = "test-dynamic-loading";
  try {
    for (let k = 0; k < 5; k++) {
      run(s, "open", "https://the-internet.herokuapp.com/dynamic_loading/2");
      const t0 = Date.now();
      const d = doJson(s, "Start the loading and wait until the result text appears");
      const ms = Date.now() - t0;
      assert.ok(["done", "likely_done"].includes(d.status), `run ${k + 1}: status ${d.status}`);
      assert.ok(ms < 8000, `run ${k + 1}: ${ms} ms`);
      assert.equal(evalJs(s, "() => document.querySelector('#finish h4')?.innerText"), "Hello World!");
    }
  } finally { run(s, "close"); }
});

// a select whose option does not exist must not touch the page, and must not claim success
test("neg-missing-option: Option 7 leaves the dropdown at \"\" and ends as not_found", live, () => {
  const s = "test-missing-option";
  try {
    run(s, "open", "https://the-internet.herokuapp.com/dropdown");
    const d = doJson(s, "Select Option 7 in the dropdown");
    assert.equal(evalJs(s, "() => document.querySelector('#dropdown').value"), "");
    assert.ok(!["done", "likely_done"].includes(d.status), `status ${d.status}`);
    assert.equal(d.status, "not_found");
    // the option that exists still works
    const ok = doJson(s, "Select Option 2 in the dropdown");
    assert.equal(evalJs(s, "() => document.querySelector('#dropdown').value"), "2");
    assert.ok(["done", "likely_done"].includes(ok.status), `status ${ok.status}`);
  } finally { run(s, "close"); }
});

// a JS dialog is a page change: the right_click's alert must reach the ending judges, so the
// action is not repeated (it was: the alert fired twice, then do ended as stuck)
test("context-menu: done or likely_done after exactly 1 right_click and 1 dialog", live, () => {
  const s = "test-context-menu";
  try {
    run(s, "open", "https://the-internet.herokuapp.com/context_menu");
    const out = run(s, "do", "Right-click inside the box to open the context menu");
    const status = out.match(/^status: (\S+)/m)?.[1];
    assert.ok(["done", "likely_done"].includes(status), `status ${status}`);
    assert.equal((out.match(/^ {2}- right_click /gm) ?? []).length, 1, out);
    assert.equal((out.match(/^ {2}~ alert dialog /gm) ?? []).length, 1, out);
  } finally { run(s, "close"); }
});
