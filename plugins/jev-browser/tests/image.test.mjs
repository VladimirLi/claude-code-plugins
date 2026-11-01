import { test } from "node:test";
import assert from "node:assert/strict";
import { Agent } from "../src/agent.mjs";

const fakeTab = (full) => ({ J: async () => full, screenshot: async ({ boxes }) => `shot:${Object.keys(boxes ?? {}).length}` });
const page = (text, elements = [], boxes = {}) => ({ url: "https://x/entry_ad", title: "The Internet", text, elements, boxes, dialogs: [], metrics: { text_length: text.length } });

test("unseen: goal words the visible page does not show (entry ad before its timer fires)", async () => {
  const a = new Agent(fakeTab(""));
  const before = page("Entry Ad Displays an ad on page load. To re-enable it, click here .", [{ id: 1, tag: "a", text: "click here" }]);
  assert.deepEqual(await a.unseen(before, "Close the advertisement modal"), ["advertisement", "modal"]);
  const after = page("This is a modal window. Close", [{ id: 5, tag: "p", text: "Close" }]);
  assert.deepEqual(await a.unseen(after, "Close the advertisement modal"), ["advertisement"]);
  assert.equal(await a.unseen(after, "Close it"), null);
  assert.equal(await a.unseen(after, "Close the modal window that says 'hello'"), null);
});

test("shot: marks every in-view element, but never shows a secret typed into a plain field", async () => {
  const a = new Agent(fakeTab(""));
  const els = [{ id: 1, tag: "input:text", label: "One-time code" }, { id: 2, tag: "input:password", label: "Password" }];
  const p = page("Sign in", els, { 1: [0, 0, 10, 10], 2: [0, 20, 10, 10] });
  assert.equal(await a.shot(p), "shot:2");
  a.typed.set(2, "password");
  assert.equal(await a.shot(p), "shot:2");
  a.typed.set(1, "otp");
  assert.equal(await a.shot(p), undefined);
});
