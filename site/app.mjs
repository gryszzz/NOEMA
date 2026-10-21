import { MAX_REPORT_BYTES, validateReport } from "./report.mjs";
const $ = (id) => document.getElementById(id);
function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
const feeds = [
  "KALSHI · markets sweep", "POLYMARKET US · CLOB", "SOLANA DEX · GeckoTerminal",
  "DEXSCREENER · pairs", "PUMP.FUN · new pairs", "NEWS · BBC", "NEWS · NPR",
  "FED · press", "SEC · press", "COURTLISTENER · SCOTUS", "GDELT · events",
  "FEDERAL REGISTER", "CONGRESS", "TEV JUDGE · self-check", "CHIEF · veto gate",
  "CAPITAL GUARD · floor 75%", "LIVE FLAGS · OFF",
];
const track = $("ticker");
for (const pass of [0, 1])
  feeds.forEach((f) => track.append(element("span", f, pass ? "dup" : "")));
const canvas = $("grid");
const ctx = canvas.getContext("2d");
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
let t = 0;
function draw() {
  const w = (canvas.width = innerWidth), h = (canvas.height = innerHeight);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(34,245,255,0.07)";
  const step = 48, off = (t * 0.4) % step;
  ctx.beginPath();
  for (let x = -off; x < w; x += step) { ctx.moveTo(x, 0); ctx.lineTo(x, h); }
  for (let y = off; y < h; y += step) { ctx.moveTo(0, y); ctx.lineTo(w, y); }
  ctx.stroke();
  for (let i = 0; i < 24; i++) {
    const x = (Math.sin(i * 12.9 + t * 0.002 * (1 + (i % 3))) * 0.5 + 0.5) * w;
    const y = (Math.cos(i * 7.3 + t * 0.0015) * 0.5 + 0.5) * h;
    ctx.fillStyle = i % 2 ? "rgba(255,43,214,0.35)" : "rgba(34,245,255,0.35)";
    ctx.fillRect(x, y, 2, 2);
  }
  t++;
  if (!still) requestAnimationFrame(draw);
}
draw();
if (still) addEventListener("resize", draw);
const io = new IntersectionObserver((entries) =>
  entries.forEach((e) => e.isIntersecting && e.target.classList.add("in")),
);
document.querySelectorAll(".section").forEach((s) => io.observe(s));
let importGeneration = 0;
$("report-file").addEventListener("change", async (e) => {
  const generation = ++importGeneration;
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > MAX_REPORT_BYTES)
      throw new Error("Report exceeds the 128 KiB limit.");
    const report = validateReport(await file.text());
    if (generation !== importGeneration) return;
    for (const [id, value] of Object.entries({
      receipts: report.cash.receipts_usd,
      expenses: report.cash.expenses_usd,
      cash: report.cash.net_cash_usd,
      estimate: report.operating_estimates.monthly_bill_usd,
      reserved: report.operating_estimates.model_reserved_usd,
      paper: report.paper.net_after_execution_costs_usd,
    }))
      $(id).textContent = value === null ? "Unknown" : value + " USD";
    $("report-source").textContent =
      "Imported snapshot · " + report.status.replaceAll("_", " ");
    $("report-period").textContent =
      "As of " +
      report.as_of +
      " · UTC month " +
      report.month_utc +
      " · not authenticated";
    $("report-gaps").replaceChildren(
      ...report.gaps.map((g) => element("li", g)),
    );
    $("report-quality").textContent =
      `Database ${report.database_present ? "present" : "absent"} in report · ${report.invalid_accounting_records} invalid cash/estimate records · ${report.paper.invalid_quotes} invalid and ${report.paper.duplicate_quotes} duplicate paper quotes · ${report.paper.settled_events_this_month} settled events. Import does not verify provenance or completeness.`;
    $("report-error").hidden = true;
    $("clear-report").disabled = false;
  } catch (error) {
    if (generation !== importGeneration) return;
    $("report-error").textContent =
      "Import rejected. " +
      error.message +
      " Any previously loaded report is unchanged.";
    $("report-error").hidden = false;
  }
  e.target.value = "";
});
$("clear-report").addEventListener("click", () => {
  ++importGeneration;
  ["receipts", "expenses", "cash", "estimate", "reserved", "paper"].forEach(
    (id) => ($(id).textContent = "Unknown"),
  );
  $("report-source").textContent = "No report loaded";
  $("report-period").textContent =
    "Read-only · local to this browser tab · never uploaded";
  $("report-gaps").replaceChildren(
    element("li", "Provider reconciliation is incomplete."),
    element("li", "Full activity-level cost attribution is unavailable."),
    element("li", "Paper returns cannot establish revenue."),
  );
  $("report-quality").textContent =
    "Report validation checks structure and accounting consistency. It does not authenticate the originating runtime.";
  $("report-error").hidden = true;
  $("clear-report").disabled = true;
  $("report-file").value = "";
});
