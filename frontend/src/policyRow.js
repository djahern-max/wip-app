import { createElement as h, useState } from "react";
import { formatMoney } from "./money.js";

// The rows of the Policy list (F04, F04.1): a key's value or "Not decided", who, when,
// the reference, and either the Decide/Change control or the sentence saying what the
// key waits for. Written with createElement rather than JSX so `node --test` can render
// the rows through react-dom/server; no DOM library is needed.
//
// F09.4 (D-47, point 3): each key is a list item drawn with the checklist's pieces from
// Home (a tick when decided, the label and its sentence, the status in words, the one
// action); deciding a key opens its form in the same item.

export const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export const REFERENCE_LABEL = "Reference (optional): where this is written down, e.g. D-04 or the engagement letter";
export const WIP_BASIS_HINT = "Tick the same cost categories that are counted in cost to date and in EAC (BLUEPRINT §8.3).";
// F08 (D-02, D-39): the two item keys.
export const DEPOSIT_HINT = "Tick every QuickBooks item a deposit invoice is written on (one per division income account).";
export const SURCHARGE_HINT = "Tick every QuickBooks item a fuel surcharge line is written on, and give the rate as a fraction (0.0500 is 5.00%). The rate may wait; it is needed for pay applications.";
export const NO_SURCHARGE = "This company charges no fuel surcharge";
export const RATE_LABEL = "Rate as a fraction (0.0500 is 5.00%)";
// F08.2 (item 3): the pick-list shows active items, narrowed by what is typed; the
// switch adds the inactive and deleted ones; a ticked item is always shown (its label,
// from the API, says "(inactive)" or "(deleted)"). The page builds no label.
export const FIND_ITEM_LABEL = "Find an item";
export const SHOW_INACTIVE_LABEL = "Show inactive items";

// An option is shown when it is active (no flag counts as active) or the switch is
// on or it is ticked, and its label contains the typed text (case does not matter).
export function visibleOptions(options, query, showInactive, ticked) {
  const needle = (query || "").trim().toLowerCase();
  return (options || []).filter((o) => {
    const inactive = o.active === false;
    if (inactive && !showInactive && !ticked.includes(o.value)) return false;
    return needle === "" || String(o.label).toLowerCase().includes(needle);
  });
}

// "0.0500" → "5.00%": digits only, no float. The API stores the rate quantized to four
// places; any plain decimal string is handled by moving the point two places.
export function percentOfFraction(rate) {
  if (rate === null || rate === undefined || rate === "") return "";
  const m = /^(\d+)(?:\.(\d*))?$/.exec(String(rate).trim());
  if (!m) return String(rate);
  const whole = m[1];
  const frac = (m[2] || "").padEnd(4, "0");
  const shifted = (whole + frac.slice(0, 2)).replace(/^0+(?=\d)/, "");
  const rest = frac.slice(2);
  return `${shifted}.${rest.length >= 2 ? rest : rest.padEnd(2, "0")}%`;
}

function itemNames(p, ids) {
  const byId = new Map((p.options || []).map((o) => [o.value, o.label]));
  return ids.map((id) => byId.get(id) || `item ${id}`).join(", ");
}

export function showValue(p, categories) {
  if (p.kind === "money") return formatMoney(p.value);
  if (p.kind === "month") return MONTHS[p.value - 1] || String(p.value);
  if (p.kind === "category_slots") {
    const names = categories.filter((c) => p.value.includes(c.slot)).map((c) => c.name);
    return names.length ? names.join(", ") : p.value.join(", ");
  }
  if (p.kind === "timezone") return p.value_label || String(p.value);
  if (p.kind === "choice") return p.value_label || String(p.value); // F07.4: the words come from the API
  if (p.kind === "item_ids") return itemNames(p, p.value.item_ids || []);
  if (p.kind === "surcharge") {
    const ids = p.value.item_ids || [];
    if (ids.length === 0 && (p.value.rate === null || p.value.rate === undefined)) return NO_SURCHARGE;
    const rate = p.value.rate ? ` at ${percentOfFraction(p.value.rate)}` : ", rate not decided";
    return `${itemNames(p, ids)}${rate}`;
  }
  return String(p.value);
}

/** Under a decided value: who decided, when, and the reference when there is one. */
export function decidedLine(p) {
  const who = p.decided_by_email ? `Decided by ${p.decided_by_email}` : "Decided";
  const when = p.decided_at ? ` on ${new Date(p.decided_at).toLocaleDateString()}` : "";
  const ref = p.decision_ref ? ` \u00b7 Reference: ${p.decision_ref}` : "";
  return `${who}${when}${ref}`;
}

function marker(p) {
  return h(
    "span",
    { className: p.decided ? "step-marker step-marker-done" : "step-marker", "aria-hidden": "true" },
    p.decided ? "\u2713" : "",
  );
}

function status(p) {
  return h("span", { className: p.decided ? "step-status step-status-done" : "step-status" }, p.decided ? "Decided" : "Not decided");
}

export function PolicyRow({ p, categories, canSetPolicy, busy, onEdit }) {
  const body = [
    h("div", { key: "label", className: "step-title" }, p.label),
    h("div", { key: "description", className: "step-message" }, p.description),
  ];
  if (p.decided) {
    body.push(
      h("div", { key: "value", className: "policy-value" }, showValue(p, categories)),
      h("div", { key: "who", className: "cell-sub wrap-anywhere" }, decidedLine(p)),
    );
  }
  if (canSetPolicy && p.waiting) body.push(h("div", { key: "waiting", className: "hint" }, p.waiting));
  return h(
    "li",
    { className: "step" },
    marker(p),
    h("div", { className: "step-body" }, ...body),
    status(p),
    h(
      "div",
      { className: "step-action" },
      canSetPolicy && !p.waiting
        ? h(
            "button",
            { type: "button", className: "link-button", disabled: busy, onClick: () => onEdit(p.key) },
            p.decided ? "Change" : "Decide",
          )
        : null,
    ),
  );
}

export function hasValue(kind, text, slots, items = [], none = false) {
  if (kind === "category_slots") return slots.length > 0;
  if (kind === "month") return text !== "";
  if (kind === "item_ids") return items.length > 0;
  if (kind === "surcharge") return none || items.length > 0;
  return text.trim() !== "";
}

// What a PUT on an item key sends (F08): the ids, and for the surcharge the rate or the
// "no surcharge" decision (no items, no rate).
export function itemValue(kind, text, items, none) {
  if (kind === "item_ids") return { item_ids: items };
  if (none) return { item_ids: [], rate: null };
  return { item_ids: items, rate: text.trim() === "" ? null : text.trim() };
}

export function EditPolicy({ p, categories, busy, onCancel, onSave }) {
  const itemKind = p.kind === "item_ids" || p.kind === "surcharge";
  const stored = p.decided && itemKind ? p.value : null;
  const [text, setText] = useState(
    p.decided && p.kind !== "category_slots" && !itemKind ? String(p.value) : stored && stored.rate ? String(stored.rate) : "",
  );
  const [slots, setSlots] = useState(p.decided && p.kind === "category_slots" ? p.value : []);
  const [items, setItems] = useState(stored ? stored.item_ids || [] : []);
  const [none, setNone] = useState(Boolean(stored && p.kind === "surcharge" && (stored.item_ids || []).length === 0 && !stored.rate));
  const [ref, setRef] = useState("");
  const [query, setQuery] = useState(""); // F08.2: the pick-list's search box
  const [showInactive, setShowInactive] = useState(false);

  function value() {
    if (p.kind === "month") return Number.isNaN(parseInt(text, 10)) ? text : parseInt(text, 10);
    if (p.kind === "category_slots") return slots;
    if (itemKind) return itemValue(p.kind, text, items, none);
    return text; // money stays a string; the server parses it as Decimal
  }

  function toggleItem(id, on) {
    setItems(on ? [...items, id] : items.filter((i) => i !== id));
  }

  let control = null;
  if (p.kind === "month") {
    control = h(
      "label",
      { className: "field" },
      h("span", { className: "field-label" }, "Month"),
      h(
        "select",
        { className: "select", value: text, onChange: (e) => setText(e.target.value), disabled: busy },
        h("option", { value: "" }, "Choose…"),
        ...MONTHS.map((m, i) => h("option", { key: m, value: i + 1 }, m)),
      ),
    );
  } else if (p.kind === "timezone" || p.kind === "choice") {
    // The options and their labels come from the API (one mapping, on the server).
    control = h(
      "label",
      { className: "field" },
      h("span", { className: "field-label" }, p.kind === "timezone" ? "Time zone" : p.label),
      h(
        "select",
        { className: "select", value: text, onChange: (e) => setText(e.target.value), disabled: busy },
        h("option", { value: "" }, "Choose…"),
        ...(p.options || []).map((o) => h("option", { key: o.value, value: o.value }, o.label)),
      ),
    );
  } else if (p.kind === "category_slots") {
    control = h(
      "fieldset",
      { className: "checks" },
      h("legend", null, "Cost categories in the WIP basis"),
      h("div", { className: "hint" }, WIP_BASIS_HINT),
      h(
        "div",
        { className: "check-grid" },
        ...categories
          .filter((c) => c.active)
          .map((c) =>
            h(
              "label",
              { key: c.id, className: "check" },
              h("input", {
                type: "checkbox",
                checked: slots.includes(c.slot),
                disabled: busy,
                onChange: (e) => setSlots(e.target.checked ? [...slots, c.slot] : slots.filter((s) => s !== c.slot)),
              }),
              `${c.slot} ${c.name}`,
            ),
          ),
      ),
    );
  } else if (itemKind) {
    const options = p.options || [];
    const shown = visibleOptions(options, query, showInactive, items);
    const picks = options.length === 0
      ? [h("div", { key: "none", className: "hint" }, "No QuickBooks items are held yet. Connect QuickBooks and run a backfill, then create the items (OPERATIONS, Jobs).")]
      : [
          h(
            "label",
            { key: "find", className: "field" },
            h("span", { className: "field-label" }, FIND_ITEM_LABEL),
            h("input", { className: "control control-wide", type: "search", value: query, onChange: (e) => setQuery(e.target.value), disabled: busy || none }),
          ),
          h(
            "label",
            { key: "inactive", className: "check" },
            h("input", { type: "checkbox", checked: showInactive, disabled: busy || none, onChange: (e) => setShowInactive(e.target.checked) }),
            SHOW_INACTIVE_LABEL,
          ),
          ...(shown.length === 0 ? [h("div", { key: "nomatch", className: "hint" }, "No item is named like that.")] : []),
          ...shown.map((o) =>
            h(
              "label",
              { key: o.value, className: "check" },
              h("input", {
                type: "checkbox",
                checked: items.includes(o.value),
                disabled: busy || none,
                onChange: (e) => toggleItem(o.value, e.target.checked),
              }),
              o.label,
            ),
          ),
        ];
    const children = [
      h("legend", null, p.kind === "item_ids" ? "QuickBooks deposit items" : "QuickBooks fuel surcharge items"),
      h("div", { className: "hint" }, p.kind === "item_ids" ? DEPOSIT_HINT : SURCHARGE_HINT),
      ...picks,
    ];
    if (p.kind === "surcharge") {
      children.push(
        h(
          "label",
          { className: "field" },
          h("span", { className: "field-label" }, RATE_LABEL),
          h("input", {
            className: "control",
            inputMode: "decimal",
            value: text,
            onChange: (e) => setText(e.target.value),
            disabled: busy || none,
          }),
        ),
        h(
          "label",
          { className: "check" },
          h("input", {
            type: "checkbox",
            checked: none,
            disabled: busy,
            onChange: (e) => {
              setNone(e.target.checked);
              if (e.target.checked) {
                setItems([]);
                setText("");
              }
            },
          }),
          NO_SURCHARGE,
        ),
      );
    }
    control = h("fieldset", { className: "checks" }, ...children);
  } else {
    control = h(
      "label",
      { className: "field" },
      h("span", { className: "field-label" }, p.kind === "money" ? "Amount" : "Value"),
      h("input", {
        className: "control",
        inputMode: p.kind === "money" ? "decimal" : "text",
        value: text,
        onChange: (e) => setText(e.target.value),
        disabled: busy,
      }),
    );
  }

  return h(
    "li",
    { className: "step step-editing" },
    marker(p),
    h(
      "div",
      { className: "step-body" },
      h("div", { className: "step-title" }, p.label),
      h("div", { className: "step-message" }, p.description),
      h(
        "div",
        { className: "policy-form" },
        control,
        h(
          "label",
          { className: "field" },
          h("span", { className: "field-label" }, REFERENCE_LABEL),
          h("input", { className: "control control-wide", value: ref, onChange: (e) => setRef(e.target.value), disabled: busy }),
        ),
        h(
          "span",
          { className: "actions actions-row" },
          h(
            "button",
            {
              type: "button",
              className: "button-primary",
              disabled: busy || !hasValue(p.kind, text, slots, items, none),
              onClick: () => onSave(p.key, value(), ref),
            },
            "Record decision",
          ),
          h("button", { type: "button", className: "link-button", disabled: busy, onClick: onCancel }, "Cancel"),
        ),
      ),
    ),
    status(p),
    h("div", { className: "step-action" }),
  );
}
