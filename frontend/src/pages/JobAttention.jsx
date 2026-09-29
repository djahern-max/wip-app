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
