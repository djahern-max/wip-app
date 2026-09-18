// The one money formatter (F03 close-out; D-22 interface conventions).
//
// Money arrives from the API as a decimal STRING and stays a string here: no
// parseFloat, no Number(), no arithmetic in binary floating point. The digits are
// handled as text, so "141366.68000000002" prints exactly as accounting would,
// "141,366.68", and the cents are never off by a rounding artefact.
//
// Rules: cents always ("0.5" → "0.50"); thousands separated by commas; negatives in
// parentheses ("(1,234.50)"); zero is "0.00" (never "-0.00"); half-up to cents when a
// value carries more than two decimals (matches ROUND_HALF_UP in app/wip/calc.py).
// Anything that is not a plain decimal string throws: a wrong type or a garbled
// value must fail loudly, never render as NaN or 0.

const DECIMAL = /^([+-])?(\d+)(?:\.(\d+))?$/;

export function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "string") {
    throw new TypeError(`formatMoney expects a decimal string, got ${typeof value}`);
  }
  const m = DECIMAL.exec(value.trim());
  if (!m) throw new TypeError("formatMoney expects a decimal string like \"1234.50\"");
  const [, sign, intPart, fracPart = ""] = m;
  // Round half up to cents using digits only (BigInt keeps arbitrary size exact).
  let cents = BigInt(intPart) * 100n + BigInt((fracPart + "00").slice(0, 2));
  if (fracPart.length > 2 && fracPart[2] >= "5") cents += 1n;
  const negative = sign === "-" && cents !== 0n;
  const digits = cents.toString().padStart(3, "0");
  const whole = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const text = `${whole}.${digits.slice(-2)}`;
  return negative ? `(${text})` : text;
}
