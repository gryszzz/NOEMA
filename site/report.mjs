// Display contract for `noema economics-report`. Imports are untrusted snapshots,
// never authenticated accounts or a source of execution authority.
export const MAX_REPORT_BYTES = 128 * 1024;
const fail = (reason) => {
  throw new Error(reason);
};
const object = (v) => v && typeof v === "object" && !Array.isArray(v);
const count = (v) => Number.isSafeInteger(v) && v >= 0;
const text = (v) => typeof v === "string" && v.length > 0 && v.length <= 2000;
// Parse Python Decimal's fixed/scientific notation into exact 18-place units.
// Bounds prevent pathological allocation and silent loss of precision.
const units = (value) => {
  if (typeof value !== "string") throw new Error("Expected a decimal string");
  const match = /^(-)?(\d{1,24})(?:\.(\d{1,18}))?(?:[eE]([+-]?\d{1,2}))?$/.exec(
    value,
  );
  if (!match) throw new Error("Invalid decimal");
  const [, negative, whole, fraction = "", exponent = "0"] = match;
  const scale = 18 + Number(exponent) - fraction.length;
  if (scale < 0 || scale > 42)
    throw new Error("Decimal precision outside supported bounds");
  return (
    BigInt(whole + fraction) * 10n ** BigInt(scale) * (negative ? -1n : 1n)
  );
};
const decimal = (value, signed = false) => {
  try {
    const amount = units(value);
    return signed || amount >= 0n;
  } catch {
    return false;
  }
};
export function validateReport(raw) {
  if (
    typeof raw !== "string" ||
    new TextEncoder().encode(raw).length > MAX_REPORT_BYTES
  )
    fail("Report exceeds the 128 KiB limit.");
  let v;
  try {
    v = JSON.parse(raw);
  } catch {
    fail("The file is not valid JSON.");
  }
  if (
    !object(v) ||
    !object(v.cash) ||
    !object(v.operating_estimates) ||
    !object(v.paper)
  )
    fail("Expected a NOEMA economics-report snapshot.");
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(
      v.as_of,
    ) ||
    !Number.isFinite(Date.parse(v.as_of)) ||
    new Date(v.as_of).toISOString().slice(0, 7) !== v.month_utc
  )
    fail("Report needs a timezone-aware date and matching UTC month.");
  const day = v.as_of.slice(0, 10);
  if (new Date(day + "T00:00:00Z").toISOString().slice(0, 10) !== day)
    fail("Invalid calendar date.");
  if (Date.parse(v.as_of) > Date.now() + 60_000)
    fail("Report timestamp is in the future.");
  if (
    typeof v.database_present !== "boolean" ||
    !count(v.invalid_accounting_records)
  )
    fail("Invalid report quality metadata.");
  if (
    v.net_economic_profit_usd !== null ||
    v.self_funding_demonstrated !== false
  )
    fail(
      "Unsupported profit claim: this contract requires unknown full profit and no demonstrated self-funding.",
    );
  const c = v.cash,
    o = v.operating_estimates,
    p = v.paper;
  if (
    !count(c.entry_count) ||
    !decimal(c.receipts_usd) ||
    !decimal(c.expenses_usd) ||
    !decimal(c.net_cash_usd, true) ||
    !text(c.basis)
  )
    fail("Invalid recorded cash values.");
  if (units(c.receipts_usd) - units(c.expenses_usd) !== units(c.net_cash_usd))
    fail("Recorded cash does not reconcile to receipts less expenses.");
  if (
    c.entry_count === 0 &&
    (units(c.receipts_usd) !== 0n || units(c.expenses_usd) !== 0n)
  )
    fail("Cash values require recorded entries.");
  if (
    (o.monthly_bill_usd !== null && !decimal(o.monthly_bill_usd)) ||
    !decimal(o.model_reserved_usd) ||
    !count(o.model_attempt_count) ||
    !text(o.basis)
  )
    fail("Invalid operating estimates.");
  if (
    !decimal(p.net_after_execution_costs_usd, true) ||
    !text(p.model_version) ||
    !text(p.basis) ||
    ![
      "settled_markets_this_month",
      "settled_events_this_month",
      "pending_markets",
      "invalid_quotes",
      "duplicate_quotes",
    ].every((k) => count(p[k])) ||
    p.settled_events_this_month > p.settled_markets_this_month
  )
    fail("Invalid paper measurement.");
  if (o.model_attempt_count === 0 && units(o.model_reserved_usd) !== 0n)
    fail("Model reservations require recorded attempts.");
  if (
    p.settled_markets_this_month === 0 &&
    units(p.net_after_execution_costs_usd) !== 0n
  )
    fail("Paper returns require settled markets.");
  if (
    !v.database_present &&
    (c.entry_count !== 0 ||
      o.monthly_bill_usd !== null ||
      o.model_attempt_count !== 0 ||
      p.settled_markets_this_month !== 0 ||
      p.pending_markets !== 0 ||
      p.invalid_quotes !== 0 ||
      p.duplicate_quotes !== 0 ||
      v.invalid_accounting_records !== 0)
  )
    fail("An absent database cannot contain measured records.");
  const status =
    v.invalid_accounting_records || p.invalid_quotes
      ? "records_invalid"
      : !c.entry_count
        ? "no_recorded_cash"
        : units(c.net_cash_usd) < 0n
          ? "recorded_cash_deficit"
          : units(c.net_cash_usd) > 0n
            ? "recorded_cash_surplus"
            : "recorded_cash_balanced";
  if (v.status !== status) fail("Report status contradicts its measurements.");
  if (
    !Array.isArray(v.gaps) ||
    v.gaps.length < 1 ||
    v.gaps.length > 30 ||
    !v.gaps.every(text)
  )
    fail("Missing or invalid reconciliation gaps.");
  return v;
}
