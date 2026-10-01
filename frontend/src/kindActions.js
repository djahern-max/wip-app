import { createElement } from "react";
import { kindActions } from "./jobs.js";

// The Action cell of a work-area row (F07.1): one button per action `kindActions`
// names, each its own element with its own name, with visible space between them
// (`.actions`). Written with createElement rather than JSX so `node --test` can render
// it through react-dom/server; no DOM library is needed.
export function KindActions({ area, busy, onConfirm }) {
  const actions = kindActions(area);
  if (actions.length === 0) return null;
  return createElement(
    "span",
    { className: "actions" },
    ...actions.map((a) =>
      createElement(
        "button",
        {
          key: a.kind,
          type: "button",
          className: "link-button",
          disabled: busy,
          onClick: () => onConfirm(a.kind),
        },
        a.label,
      ),
    ),
  );
}
