import { dismissedLine } from "../exceptions.js";

// The sentences, never the codes (D-22). "None" is a word, so an empty cell is never
// mistaken for a missing value.
export default function Attention({ items }) {
  if (!items || items.length === 0) return <span className="muted">None</span>;
  return (
    <ul className="issues">
      {items.map((i, n) => (
        <li key={n}>{i.message}</li>
      ))}
    </ul>
  );
}

// F09 (D-46): the subject's dismissed exceptions, never shown as needs; each with the
// sentence as last raised and who dismissed it, when, with the note.
export function Dismissed({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <>
      <h4>Dismissed</h4>
      <ul className="issues">
        {items.map((d) => (
          <li key={d.id}>
            {d.message} <span className="muted">{dismissedLine(d)}</span>
          </li>
        ))}
      </ul>
    </>
  );
}

