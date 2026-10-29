// Live tests for the irreversible gate on form submits: Enter in a field, Enter or Space on the
// focused element, and a page script that submits the form. They drive the real daemon and
// Chrome (the act cases need no Jev call), so they run only with JEVB_LIVE=1:
//   JEVB_LIVE=1 node --test tests/irreversible.live.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const JEVB = fileURLToPath(new URL("../bin/jevb", import.meta.url));
const live = { skip: process.env.JEVB_LIVE !== "1" && "set JEVB_LIVE=1 (needs Chrome; the do case also needs the Jev key)" };
const run = (session, ...args) => execFileSync(JEVB, ["--session", session, ...args], { encoding: "utf8", timeout: 120_000 });
const status = (out) => out.match(/^status: (\S+)/m)?.[1];
const placed = (s) => JSON.parse(run(s, "eval", "() => window.placed"));

// "Next" is a plain button whose script calls requestSubmit(): no label rule sees the commit
const CHECKOUT = "data:text/html," + encodeURIComponent(`<title>Checkout</title><h1>Order total $ 32.39</h1>
<form id="f"><label>Card number <input id="card"></label> <button>Place order</button></form>
<button type="button" id="next" onclick="document.getElementById('f').requestSubmit()">Next</button>
<div id="out"></div>
<script>window.placed = false; f.addEventListener("submit", (e) => { e.preventDefault(); window.placed = true; out.textContent = "Order placed"; });</script>`);

function openCheckout(s) {
  const out = run(s, "open", CHECKOUT);
  const id = (re) => Number(out.match(new RegExp(`\\[(\\d+)\\] ${re}`))?.[1]);
  return { card: id('input:text label="Card number'), place: id('button "Place order"'), next: id('button "Next"') };
}

test("checkout: every way to submit the order pauses with needs_confirmation", live, () => {
  const s = "test-irreversible";
  try {
    let ids = openCheckout(s);
    assert.ok(ids.card && ids.place && ids.next, JSON.stringify(ids));
    for (const args of [["click", ids.place], ["press_enter", ids.card], ["press_key", ids.card, "Enter"]]) {
      const out = run(s, "act", ...args.map(String));
      assert.equal(status(out), "needs_confirmation", `${args.join(" ")}\n${out}`);
      assert.match(out, /^resume: jevb act .* --allow-irreversible/m);
      assert.equal(placed(s), false, args.join(" "));
    }
    // Enter with no target goes to the focused field
    run(s, "act", "type", String(ids.card), "4111");
    let out = run(s, "act", "press_key", "Enter");
    assert.equal(status(out), "needs_confirmation", out);
    assert.equal(placed(s), false);
    // the page's own script submits: the in-page guard stops it
    out = run(s, "act", "click", String(ids.next));
    assert.equal(status(out), "needs_confirmation", out);
    assert.match(out, /submit was stopped/);
    assert.equal(placed(s), false);
    // allow_irreversible lets it through
    out = run(s, "act", "press_enter", String(ids.card), "--allow-irreversible");
    assert.equal(status(out), "ok", out);
    assert.equal(placed(s), true);
    // and the guard is armed again after the allowed call
    ids = openCheckout(s);
    out = run(s, "act", "click", String(ids.next));
    assert.equal(status(out), "needs_confirmation", out);
    assert.equal(placed(s), false);
  } finally { run(s, "close"); }
});

test("checkout: do never places the order without allow_irreversible", live, () => {
  const s = "test-irreversible-do";
  try {
    openCheckout(s);
    const out = run(s, "do", "Enter the card number and press Enter", "--values", JSON.stringify({ card_number: "4111111111111111" }));
    assert.equal(status(out), "needs_confirmation", out);
    assert.equal(placed(s), false);
  } finally { run(s, "close"); }
});

// A Swedish booking form: no button word is on the English list, but the form collects contact
// details (by input type, and by attribute words for the address), so every submit pauses.
const BOOKING = "data:text/html," + encodeURIComponent(`<meta charset="utf-8"><title>Boka provstädning</title><h1>Boka din provstädning</h1>
<form id="f"><label>Namn <input name="namn"></label> <label>E-post <input type="email" name="epost"></label>
<label>Telefon <input type="tel" name="telefon"></label> <label>Adress <input name="adress"></label>
<button>Boka din provstädning nu</button> <input type="submit" value="Skicka bokning"></form>
<button type="button" id="next" onclick="document.getElementById('f').requestSubmit()">Nästa</button>
<form id="g"><label>Användarnamn <input name="username"></label> <label>Lösenord <input type="password" name="password"></label> <button>Logga in</button></form>
<div id="out"></div>
<script>window.placed = false; window.loggedIn = false; f.addEventListener("submit", (e) => { e.preventDefault(); window.placed = true; out.textContent = "Tack för din bokning"; });
g.addEventListener("submit", (e) => { e.preventDefault(); window.loggedIn = true; out.textContent = "Inloggad"; });</script>`);

function openBooking(s) {
  run(s, "open", BOOKING);
  const out = run(s, "observe", "full");
  const id = (re) => Number(out.match(new RegExp(`\\[(\\d+)\\] ${re}`))?.[1]);
  return { email: id('input:email label="E-post'), boka: id('button "Boka din'), skicka: id('input:submit "Skicka bokning'), next: id('button "Nästa'), login: id('button "Logga in') };
}

test("booking form in Swedish: submitting contact details pauses; a login form beside it does not", live, () => {
  const s = "test-booking";
  try {
    const ids = openBooking(s);
    assert.ok(Object.values(ids).every(Boolean), JSON.stringify(ids));
    for (const args of [["click", ids.boka], ["click", ids.skicka], ["press_enter", ids.email]]) {
      const out = run(s, "act", ...args.map(String));
      assert.equal(status(out), "needs_confirmation", `${args.join(" ")}\n${out}`);
      assert.match(out, /collects contact or payment details/);
      assert.equal(placed(s), false, args.join(" "));
    }
    // the page's own script submits: the in-page guard stops it too
    let out = run(s, "act", "click", String(ids.next));
    assert.equal(status(out), "needs_confirmation", out);
    assert.match(out, /submit was stopped/);
    assert.equal(placed(s), false);
    // the login form collects no contact details: it submits
    out = run(s, "act", "click", String(ids.login));
    assert.equal(status(out), "ok", out);
    assert.equal(JSON.parse(run(s, "eval", "() => window.loggedIn")), true);
    out = run(s, "act", "click", String(ids.skicka), "--allow-irreversible");
    assert.equal(status(out), "ok", out);
    assert.equal(placed(s), true);
  } finally { run(s, "close"); }
});

test("booking form in Swedish: do fills it in but never sends it without allow_irreversible", live, () => {
  const s = "test-booking-do";
  try {
    run(s, "open", BOOKING);
    const out = run(s, "do", "Fill in the booking form and book the cleaning", "--values", JSON.stringify({ name: "Ada Lovelace", email: "ada@example.com", phone: "0701234567", address: "Storgatan 1" }));
    assert.equal(status(out), "needs_confirmation", out);
    assert.equal(placed(s), false);
  } finally { run(s, "close"); }
});
