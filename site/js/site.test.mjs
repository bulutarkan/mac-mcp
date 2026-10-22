import test from "node:test";
import assert from "node:assert/strict";
import { copyText, formatStars } from "./site.js";

test("copies the target's text and says so", async () => {
  const button = { dataset: { copy: "pkg-sha" }, textContent: "Copy" };
  let written = null;
  const ok = await copyText(button, (id) => (id === "pkg-sha" ? "abc123" : null), async (text) => { written = text; });
  assert.equal(ok, true);
  assert.equal(written, "abc123");
  assert.equal(button.textContent, "Copied");
});

test("asks to select the text when the clipboard refuses", async () => {
  const button = { dataset: { copy: "installer" }, textContent: "Copy" };
  const ok = await copyText(button, () => "curl …", () => Promise.reject(new Error("denied")));
  assert.equal(ok, false);
  assert.equal(button.textContent, "Select it");
});

test("does nothing without a target", async () => {
  const button = { dataset: { copy: "missing" }, textContent: "Copy" };
  assert.equal(await copyText(button, () => null, async () => {}), false);
  assert.equal(button.textContent, "Copy");
});

test("shows the star count the way GitHub does", () => {
  assert.equal(formatStars(88), "88");
  assert.equal(formatStars(999), "999");
  assert.equal(formatStars(1000), "1k");
  assert.equal(formatStars(1450), "1.4k");
  assert.equal(formatStars(12400), "12k");
});
