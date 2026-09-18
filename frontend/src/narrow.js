import { useEffect, useState } from "react";

// True below `maxWidth` px (phone width; the Imports page switches from a table
// to stacked cards so nothing forces a sideways page scroll at 390 px).
export function useNarrow(maxWidth = 640) {
  const query = `(max-width: ${maxWidth}px)`;
  const [narrow, setNarrow] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (e) => setNarrow(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return narrow;
}
