import { inactiveLabel } from "../../inactive.js";

// "Show inactive (n)" above a configuration list (F06.1): page state only; nothing is
// deleted or changed. Rendered only when there is something to show.
export default function ShowInactive({ count, showing, onToggle }) {
  if (count === 0 && !showing) return null;
  return (
    <p className="show-inactive">
      <button type="button" className="link-button" aria-pressed={showing} onClick={onToggle}>
        {inactiveLabel(count, showing)}
      </button>
    </p>
  );
}
