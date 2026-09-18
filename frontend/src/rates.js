// Rates as text (D-22): a burden rate arrives as a decimal-fraction STRING such as
// "0.3250" and is shown as a percentage with two decimals, "32.50%". Digits are
// handled as text and BigInt, never parseFloat/Number, like money.js.

const DECIMAL = /^([+-])?(\d+)(?:\.(\d+))?$/;

export function formatRate(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "string") throw new TypeError("formatRate expects a decimal string");
  const m = DECIMAL.exec(value.trim());
  if (!m) throw new TypeError('formatRate expects a decimal string like "0.3250"');
  const [, sign, intPart, fracPart = ""] = m;
  // percent = value × 100, kept to two decimals (half up): that is four fraction digits.
  const frac = (fracPart + "0000").slice(0, 4);
  let hundredths = BigInt(intPart + frac); // value × 10 000
  if (fracPart.length > 4 && fracPart[4] >= "5") hundredths += 1n;
  const negative = sign === "-" && hundredths !== 0n;
  const digits = hundredths.toString().padStart(3, "0");
  const whole = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const text = `${whole}.${digits.slice(-2)}%`;
  return negative ? `(${text})` : text;
}
