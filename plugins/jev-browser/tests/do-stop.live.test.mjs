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
