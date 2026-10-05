import { createElement as h, useState } from "react";
import { formatMoney } from "./money.js";

// The rows of the Policy table (F04, F04.1): a key's value or "Not decided", who, when,
// the reference, and either the Decide/Change control or the sentence saying what the
// key waits for. Written with createElement rather than JSX so `node --test` can render
// the rows through react-dom/server; no DOM library is needed.

export const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export const REFERENCE_LABEL = "Reference (optional): where this is written down, e.g. D-04 or the engagement letter";
export const WIP_BASIS_HINT = "Tick the same cost categories that are counted in cost to date and in EAC (BLUEPRINT §8.3).";

export function showValue(p, categories) {
  if (p.kind === "money") return formatMoney(p.value);
  if (p.kind === "month") return MONTHS[p.value - 1] || String(p.value);
  if (p.kind === "category_slots") {
    const names = categories.filter((c) => p.value.includes(c.slot)).map((c) => c.name);
    return names.length ? names.join(", ") : p.value.join(", ");
  }
  if (p.kind === "timezone") return p.value_label || String(p.value);
  return String(p.value);
}

export function PolicyRow({ p, categories, canSetPolicy, busy, onEdit }) {
  // `.num` right-aligns a decided money value only; "Not decided" lines up on the left.
  const valueClass = p.kind === "money" && p.decided ? "num" : undefined;
  const cells = [
    h("td", { key: "label" }, p.label, h("div", { className: "hint" }, p.description)),
    h("td", { key: "value", className: valueClass }, p.decided ? showValue(p, categories) : "Not decided"),
    h("td", { key: "who" }, p.decided_by_email || ""),
    h("td", { key: "when" }, p.decided_at ? new Date(p.decided_at).toLocaleDateString() : ""),
    h("td", { key: "ref", className: "wrap-anywhere" }, p.decision_ref || ""),
  ];
  if (canSetPolicy) {
    cells.push(
      h(
        "td",
        { key: "action" },
        p.waiting
          ? h("span", { className: "hint" }, p.waiting)
          : h(
              "button",
              { type: "button", className: "link-button", disabled: busy, onClick: () => onEdit(p.key) },
              p.decided ? "Change" : "Decide",
            ),
      ),
    );
  }
  return h("tr", null, ...cells);
}

export function hasValue(kind, text, slots) {
  if (kind === "category_slots") return slots.length > 0;
  if (kind === "month") return text !== "";
  return text.trim() !== "";
}

export function EditPolicy({ p, categories, busy, onCancel, onSave }) {
  const [text, setText] = useState(p.decided && p.kind !== "category_slots" ? String(p.value) : "");
  const [slots, setSlots] = useState(p.decided && p.kind === "category_slots" ? p.value : []);
  const [ref, setRef] = useState("");

  function value() {
    if (p.kind === "month") return Number.isNaN(parseInt(text, 10)) ? text : parseInt(text, 10);
    if (p.kind === "category_slots") return slots;
    return text; // money stays a string; the server parses it as Decimal
  }

  let control = null;
  if (p.kind === "month") {
    control = h(
      "label",
      { className: "label" },
      "Month",
      h(
        "select",
        { className: "input", value: text, onChange: (e) => setText(e.target.value), disabled: busy },
        h("option", { value: "" }, "Choose…"),
        ...MONTHS.map((m, i) => h("option", { key: m, value: i + 1 }, m)),
      ),
    );
  } else if (p.kind === "timezone") {
    // The options and their labels come from the API (one mapping, on the server).
    control = h(
      "label",
      { className: "label" },
      "Time zone",
      h(
        "select",
        { className: "input", value: text, onChange: (e) => setText(e.target.value), disabled: busy },
        h("option", { value: "" }, "Choose…"),
        ...(p.options || []).map((o) => h("option", { key: o.value, value: o.value }, o.label)),
      ),
    );
  } else if (p.kind === "category_slots") {
    control = h(
      "fieldset",
      { className: "label" },
      h("legend", null, "Cost categories in the WIP basis"),
      h("div", { className: "hint" }, WIP_BASIS_HINT),
      ...categories
        .filter((c) => c.active)
        .map((c) =>
          h(
            "label",
            { key: c.id, className: "small" },
            h("input", {
              type: "checkbox",
              checked: slots.includes(c.slot),
              disabled: busy,
              onChange: (e) => setSlots(e.target.checked ? [...slots, c.slot] : slots.filter((s) => s !== c.slot)),
            }),
            " ",
            `${c.slot} ${c.name}`,
            h("br"),
          ),
        ),
    );
  } else {
    control = h(
      "label",
      { className: "label" },
      p.kind === "money" ? "Amount" : "Value",
      h("input", {
        className: "input",
        inputMode: p.kind === "money" ? "decimal" : "text",
        value: text,
        onChange: (e) => setText(e.target.value),
        disabled: busy,
      }),
    );
  }

  return h(
    "tr",
    null,
    h("td", null, p.label, h("div", { className: "hint" }, p.description)),
    h("td", null, control),
    h(
      "td",
      { colSpan: 3 },
      h(
        "label",
        { className: "label" },
        REFERENCE_LABEL,
        h("input", { className: "input", value: ref, onChange: (e) => setRef(e.target.value), disabled: busy }),
      ),
    ),
    h(
      "td",
      null,
      h(
        "button",
        {
          type: "button",
          className: "button-primary",
          disabled: busy || !hasValue(p.kind, text, slots),
          onClick: () => onSave(p.key, value(), ref),
        },
        "Record decision",
      ),
      " ",
      h("button", { type: "button", className: "link-button", disabled: busy, onClick: onCancel }, "Cancel"),
    ),
  );
}
