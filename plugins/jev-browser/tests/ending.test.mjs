import { test } from "node:test";
import assert from "node:assert/strict";
import { arrivedAt, scrollNames } from "../src/agent.mjs";

const page = (url, title, text = "") => ({ url, title, text, elements: [] });
const click = (name, url, extra = {}) => ({ action: "click", element: `a "${name.slice(0, 50)}"`, tag: "a", name, url, ...extra });

test("arrivedAt: a link click that landed on the page its link names", () => {
  const book = page("https://books.toscrape.com/catalogue/full-moon-over-noahs-ark-an-odyssey-to-mount-ararat-and-beyond_811/index.html", "Full Moon over Noah’s Ark: An Odyssey to Mount Ararat and Beyond | Books to Scrape - Sandbox");
  const travel = "https://books.toscrape.com/catalogue/category/books/travel_2/index.html";
  assert.ok(arrivedAt(book, [click("Travel", "https://books.toscrape.com/"), click("Full Moon over Noah’s Ark: An ...", travel)]));
  assert.ok(arrivedAt(page(travel, "Travel | Books to Scrape - Sandbox"), [click("Travel", "https://books.toscrape.com/")]));
  // no navigation, an error, a button, or a page that is not the one the link names
  assert.equal(arrivedAt(page(travel, "Travel | Books"), [click("Travel", travel)]), null);
  assert.equal(arrivedAt(book, [click("Full Moon over Noah’s Ark", travel, { error: "covered" })]), null);
  assert.equal(arrivedAt(book, [click("Full Moon over Noah’s Ark", travel, { tag: "button" })]), null);
  assert.equal(arrivedAt(page("https://x/login", "Sign in"), [click("Pricing", "https://x/")]), null);
  assert.equal(arrivedAt(book, []), null);
});

test("scrollNames: the place a scroll goal names", () => {
  assert.deepEqual(scrollNames("Scroll down to the customer reviews section"), ["customer", "reviews"]);
  assert.deepEqual(scrollNames("Scroll to the footer"), ["footer"]);
  assert.equal(scrollNames("Scroll down to the bottom of the page"), null);
  assert.equal(scrollNames("Scroll down to the Bletchley Park link and open it"), null);
  assert.equal(scrollNames("Add elements until there are exactly 3 Delete buttons"), null);
});
