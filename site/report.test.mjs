import test from "node:test";
import assert from "node:assert/strict";
import { validateReport, MAX_REPORT_BYTES } from "./report.mjs";
const report = () => ({
  as_of: "2026-01-20T12:00:00+00:00",
  month_utc: "2026-01",
  database_present: true,
  status: "recorded_cash_deficit",
  cash: {
    basis: "operator-reported, unreconciled",
    entry_count: 2,
    receipts_usd: "0.10",
    expenses_usd: "0.30",
    net_cash_usd: "-0.20",
  },
  operating_estimates: {
    monthly_bill_usd: null,
    model_reserved_usd: "0.001",
    model_attempt_count: 1,
    basis: "estimates not invoices",
  },
  paper: {
    model_version: "test-only",
    settled_markets_this_month: 2,
    settled_events_this_month: 1,
    net_after_execution_costs_usd: "1000.00",
    pending_markets: 0,
    invalid_quotes: 0,
    duplicate_quotes: 1,
    basis: "hypothetical fills",
  },
  invalid_accounting_records: 0,
  net_economic_profit_usd: null,
  self_funding_demonstrated: false,
  gaps: ["Unreconciled fixture; not real financial data."],
});
const validate = (value) => validateReport(JSON.stringify(value));
test("preserves exact decimals and keeps paper returns separate from cash", () => {
  const v = validate(report());
  assert.equal(v.cash.net_cash_usd, "-0.20");
  assert.equal(v.paper.net_after_execution_costs_usd, "1000.00");
  assert.equal(v.net_economic_profit_usd, null);
  assert.equal(v.self_funding_demonstrated, false);
});
test("accepts explicit missing database and no recorded cash", () => {
  const v = report();
  v.database_present = false;
  v.operating_estimates.model_attempt_count = 0;
  v.operating_estimates.model_reserved_usd = "0";
  v.paper.settled_markets_this_month =
    v.paper.settled_events_this_month =
    v.paper.duplicate_quotes =
      0;
  v.paper.net_after_execution_costs_usd = "0";
  v.cash.entry_count = 0;
  v.cash.receipts_usd = v.cash.expenses_usd = v.cash.net_cash_usd = "0";
  v.status = "no_recorded_cash";
  assert.equal(validate(v).database_present, false);
});
test("rejects unsupported profit claims", () => {
  const v = report();
  v.net_economic_profit_usd = "999";
  assert.throws(() => validate(v), /profit claim/);
});
test("rejects self-funding claims even with unknown profit", () => {
  const v = report();
  v.self_funding_demonstrated = true;
  assert.throws(() => validate(v), /self-funding/);
});
test("rejects inconsistent cash arithmetic", () => {
  const v = report();
  v.cash.net_cash_usd = "0.2";
  assert.throws(() => validate(v), /reconcile/);
});
test("rejects cash without recorded entries", () => {
  const v = report();
  v.cash.entry_count = 0;
  assert.throws(() => validate(v), /recorded entries/);
});
test("rejects numeric money and nonfinite strings", () => {
  for (const value of [NaN, Infinity, "NaN", "Infinity", 0.1]) {
    const v = report();
    v.cash.receipts_usd = value;
    assert.throws(() => validate(v), /cash values/);
  }
});
test("rejects negative counts and expenses", () => {
  const v = report();
  v.cash.expenses_usd = "-1";
  assert.throws(() => validate(v));
  v.cash.expenses_usd = "0.3";
  v.paper.duplicate_quotes = -1;
  assert.throws(() => validate(v), /paper/);
});
test("rejects event counts exceeding settled markets", () => {
  const v = report();
  v.paper.settled_events_this_month = 3;
  assert.throws(() => validate(v), /paper/);
});
test("requires invalid-record status when invalid quotes exist", () => {
  const v = report();
  v.paper.invalid_quotes = 1;
  assert.throws(() => validate(v), /status/);
  v.status = "records_invalid";
  assert.equal(validate(v).status, "records_invalid");
});
test("requires timezone and matching UTC month", () => {
  for (const date of [
    "not a date",
    "2026-01-20T12:00:00",
    "2026-02-01T00:00:00Z",
  ]) {
    const v = report();
    v.as_of = date;
    assert.throws(() => validate(v), /date/);
  }
});
test("rejects future snapshots", () => {
  const v = report();
  v.as_of = "2999-01-01T00:00:00Z";
  v.month_utc = "2999-01";
  assert.throws(() => validate(v), /future/);
});
test("requires explicit reconciliation gaps", () => {
  const v = report();
  v.gaps = [];
  assert.throws(() => validate(v), /gaps/);
});
test("rejects huge files, malformed JSON, null, and wrong shape", () => {
  for (const raw of [" ".repeat(MAX_REPORT_BYTES + 1), "{", "null", "[]", "{}"])
    assert.throws(() => validateReport(raw));
});
test("preserves untrusted text as inert data, without treating it as instructions", () => {
  const v = report();
  v.gaps = ["<img src=x onerror=alert(1)>"];
  assert.equal(validate(v).gaps[0], v.gaps[0]);
});

test("accepts bounded Python Decimal scientific notation without rounding", () => {
  const v = report();
  v.cash.receipts_usd = "1E-8";
  v.cash.expenses_usd = "3E-8";
  v.cash.net_cash_usd = "-2E-8";
  assert.equal(validate(v).cash.net_cash_usd, "-2E-8");
  v.cash.net_cash_usd = "-1E-8";
  assert.throws(() => validate(v), /reconcile/);
});
test("rejects normalized impossible calendar days and excessive precision", () => {
  const v = report();
  v.as_of = "2026-02-30T12:00:00Z";
  v.month_utc = "2026-03";
  assert.throws(() => validate(v), /calendar/);
  v.as_of = "2026-01-20T12:00:00Z";
  v.month_utc = "2026-01";
  v.cash.receipts_usd = "1E-99";
  assert.throws(() => validate(v), /cash values/);
});

test("rejects an absent database with measured records", () => {
  const v = report();
  v.database_present = false;
  assert.throws(() => validate(v), /absent database/);
});
test("rejects reservations without attempts", () => {
  const v = report();
  v.operating_estimates.model_attempt_count = 0;
  assert.throws(() => validate(v), /attempts/);
});
test("rejects paper returns without settlements", () => {
  const v = report();
  v.paper.settled_events_this_month = 0;
  v.paper.settled_markets_this_month = 0;
  assert.throws(() => validate(v), /settled markets/);
});
