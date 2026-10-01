import { test } from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { KindActions } from "./kindActions.js";

// F07.1: the Action cell renders each action as its own element with its own name,
// never two names run together in one control.

function buttons(html) {
  return [...html.matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)].map((m) => ({
    attrs: m[1],
    name: m[2],
  }));
}

function render(area, busy = false) {
  return renderToStaticMarkup(createElement(KindActions, { area, busy, onConfirm: () => {} }));
}

test("an unconfirmed kept row: two separate buttons, each with its own name", () => {
  const html = render({ kept: true, kind: null });
  const found = buttons(html);
  assert.deepEqual(
    found.map((b) => b.name),
    ["Confirm original", "Confirm change order"],
  );
  assert.ok(found.every((b) => b.attrs.includes('type="button"')));
  assert.ok(html.startsWith('<span class="actions">'));
});

test("a confirmed row: one control that says what it changes to", () => {
  assert.deepEqual(
    buttons(render({ kept: true, kind: "original" })).map((b) => b.name),
    ["Change to change order"],
  );
  assert.deepEqual(
    buttons(render({ kept: true, kind: "change_order" })).map((b) => b.name),
    ["Change to original"],
  );
});

test("an omitted row renders nothing; a request in flight disables the controls", () => {
  assert.equal(render({ kept: false, kind: null }), "");
  assert.ok(buttons(render({ kept: true, kind: null }, true)).every((b) => b.attrs.includes("disabled")));
});
