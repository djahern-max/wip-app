import assert from "node:assert/strict";
import { test } from "node:test";
import { chooseSource, rememberSource, rememberedSource } from "./sourceChoice.js";

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

test("a source the server no longer offers falls back to the default", () => {
  assert.equal(chooseSource(KINDS, "gone", "also_gone"), "unparsed_file");
  assert.equal(chooseSource([{ name: "x" }], "gone", null), "x");
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
