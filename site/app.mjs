import { MAX_REPORT_BYTES, validateReport } from "./report.mjs";
const $ = (id) => document.getElementById(id);
const lifecycle = {
  observe: [
    "Freeze the information boundary.",
    "Public observations, market snapshots, timestamps, and source identity come first. Missing or stale data remains unavailable.",
    [
      "Preserve what was knowable at decision time",
      "Keep source observations separate from model inference",
      "Record an immutable forecast before its outcome",
    ],
  ],
  infer: [
    "An interesting idea is a candidate.",
    "Optional cognition can challenge an opportunity and propose an investigation. Market consensus and simple historical baselines remain visible.",
    [
      "Account for model cost before the call",
      "Compare alternative explanations",
      "Keep model output away from unrestricted funds",
    ],
  ],
  verify: [
    "Ask what would invalidate it.",
    "Evaluate on later resolved events. Count distinct evidence, measure calibration, and account for execution assumptions before considering an edge.",
    [
      "No repeated promotion on the same sample",
      "No lookahead from future outcomes",
      "Paper fills remain hypothetical",
    ],
  ],
  act: [
    "Choose only what is permitted.",
    "PASS, bounded research, and paper experiments are useful outcomes. Financial execution is a separate, explicitly authorized workflow.",
    [
      "Deterministic limits precede any authorized order",
      "A mature researcher does not inherit financial authority",
      "Reconciliation and recovery remain necessary",
    ],
  ],
};
const specialists = {
  challenger: [
    "Start small. Make the hypothesis falsifiable.",
    "A challenger enters with a bounded research budget and a versioned experiment. Its comparison includes simple baselines and abstention—not just the incumbent specialist.",
  ],
  evaluation: [
    "Resolve evidence before promotion.",
    "Calibration and after-cost outcomes matter. New, distinct resolved events are required for progress; a few wins or a repeated evaluation do not establish skill.",
  ],
  allocation: [
    "Allocate attention, not imaginary profit.",
    "Research calls, tokens, and estimated cost are reserved before work begins. Failed attempts still consume their reservations. Internal allocation is not an audited cash balance.",
  ],
  quarantine: [
    "Failure should change the system.",
    "Weak or invalid evidence can quarantine a specialist. A revised challenger needs a new version, a measurable experiment, and the same evidence gates.",
  ],
};
const nodes = {
  meridian: [
    "WORLD CONTEXT",
    "Evidence with an identity.",
    "Meridian preserves source context in a frozen research request. NOEMA returns a deterministic review bound to its hash. File export and device recovery work today; a hosted durable job service is not yet implemented.",
    "Explore the bridge contract",
    "https://github.com/gryszzz/NOEMA/blob/main/docs/meridian-noema-contract.md",
  ],
  research: [
    "RESEARCH WORKER",
    "Bounded autonomy.",
    "The Python worker collects public observations, records forecasts, and can use configured cognition. Today the implemented venue loop centers on Kalshi paper research; the long-term opportunity model is broader than one venue.",
    "Read the worker guide",
    "https://github.com/gryszzz/NOEMA/blob/main/docs/agent-runtime.md",
  ],
  evaluation: [
    "EVIDENCE AND ECONOMICS",
    "Outcomes close the loop.",
    "SQLite stores forecasts, decisions, outcomes, and resource records. Evaluators enforce chronological evidence and promotion gates. The economics report keeps cash, estimates, and paper results separate.",
    "Read economic integrity",
    "https://github.com/gryszzz/NOEMA/blob/main/docs/economic-integrity.md",
  ],
  execution: [
    "AUTHORITY BOUNDARY",
    "Explicitly authorized, independently checked.",
    "Live-value execution requires a supported venue adapter, deterministic risk policy, credentials, and explicit authorization. Public Pages cannot activate it. Multi-venue execution and complete reconciliation are unfinished.",
    "Inspect the operating contract",
    "https://github.com/gryszzz/NOEMA/blob/main/AGENTS.md",
  ],
};
function element(tag, value, className) {
  const el = document.createElement(tag);
  el.textContent = value;
  if (className) el.className = className;
  return el;
}
function choose(attribute, key) {
  document
    .querySelectorAll(`[${attribute}]`)
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(b.getAttribute(attribute) === key)),
    );
}
function showStep(key) {
  const [title, body, checks] = lifecycle[key];
  choose("data-step", key);
  const copy = element("div", "");
  copy.append(element("h3", title), element("p", body));
  const list = element("ul", "");
  checks.forEach((c) => list.append(element("li", c)));
  $("lifecycle-detail").replaceChildren(copy, list);
}
function showSpecialist(key) {
  choose("data-specialist", key);
  const [title, body] = specialists[key];
  $("specialist-detail").replaceChildren(
    element("h3", title),
    element("p", body),
  );
}
function showNode(key) {
  choose("data-node", key);
  const [label, title, body, link, url] = nodes[key];
  const a = element("a", link + " ↗");
  a.href = url;
  $("architecture-detail").replaceChildren(
    element("p", label, "eyebrow"),
    element("h3", title),
    element("p", body),
    a,
  );
}
document
  .querySelectorAll("[data-step]")
  .forEach((b) => b.addEventListener("click", () => showStep(b.dataset.step)));
document
  .querySelectorAll("[data-specialist]")
  .forEach((b) =>
    b.addEventListener("click", () => showSpecialist(b.dataset.specialist)),
  );
document
  .querySelectorAll("[data-node]")
  .forEach((b) => b.addEventListener("click", () => showNode(b.dataset.node)));
showStep("observe");
showSpecialist("challenger");
showNode("research");
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
