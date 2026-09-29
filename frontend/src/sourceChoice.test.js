import assert from "node:assert/strict";
import { test } from "node:test";
import { NO_SOURCE, chooseSource, rememberSource, rememberedSource } from "./sourceChoice.js";

function memoryStore() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
}

const KINDS = [{ name: "unparsed_file" }, { name: "chart_of_accounts" }];

test("the chosen source is read back for the same company and not for another", () => {
  const s = memoryStore();
  rememberSource(s, "tenant-a", "chart_of_accounts");
  assert.equal(rememberedSource(s, "tenant-a"), "chart_of_accounts");
  assert.equal(rememberedSource(s, "tenant-b"), null);
});

test("a remounted screen shows the remembered source, not the default", () => {
  const s = memoryStore();
  rememberSource(s, "tenant-a", "chart_of_accounts");
  // a fresh mount: no current choice, kinds not loaded yet, then loaded
  const first = chooseSource([], rememberedSource(s, "tenant-a"), null);
  assert.equal(first, "chart_of_accounts");
  assert.equal(chooseSource(KINDS, first, rememberedSource(s, "tenant-a")), "chart_of_accounts");
});

test("a source the server no longer offers falls back to Choose a source", () => {
  assert.equal(chooseSource(KINDS, "gone", "also_gone"), NO_SOURCE);
  assert.equal(chooseSource([{ name: "x" }], "gone", null), NO_SOURCE);
});

test("the first visit per company per session opens on Choose a source", () => {
  const s = memoryStore();
  assert.equal(chooseSource([], rememberedSource(s, "tenant-a"), null), NO_SOURCE);
  assert.equal(chooseSource(KINDS, NO_SOURCE, rememberedSource(s, "tenant-a")), NO_SOURCE);
  rememberSource(s, "tenant-a", "chart_of_accounts");
  assert.equal(chooseSource(KINDS, NO_SOURCE, rememberedSource(s, "tenant-a")), "chart_of_accounts");
  assert.equal(chooseSource(KINDS, NO_SOURCE, rememberedSource(s, "tenant-b")), NO_SOURCE);
  rememberSource(s, "tenant-a", NO_SOURCE); // choosing nothing is never remembered
  assert.equal(rememberedSource(s, "tenant-a"), "chart_of_accounts");
});

test("storage that throws or is missing never breaks the page", () => {
  const broken = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  rememberSource(broken, "tenant-a", "chart_of_accounts");
  assert.equal(rememberedSource(broken, "tenant-a"), null);
  assert.equal(rememberedSource(null, "tenant-a"), null);
});
