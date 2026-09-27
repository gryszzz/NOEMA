import assert from "node:assert/strict";
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { serveBuild } from "./serve-build.mjs";
const base = process.env.SITE_BASE_PATH ?? "/NOEMA/";
const { server, url } = await serveBuild("dist-site", base);
const browser = await chromium.launch({
  headless: true,
  args: ["--enable-unsafe-swiftshader"],
});
const errors = [],
  failed = [],
  external = [];
try {
  for (const width of [390, 768, 1024, 1440]) {
    const page = await browser.newPage({
      viewport: { width, height: 1000 },
      reducedMotion: "reduce",
    });
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("response", (r) => {
      if (r.status() >= 400) failed.push(r.url());
    });
    page.on("request", (r) => {
      if (!r.url().startsWith(url)) external.push(r.url());
    });
    await page.goto(url + "#method", { waitUntil: "networkidle" });
    assert.equal(await page.locator("h1").count(), 1);
    for (const step of ["observe", "infer", "verify", "act"]) {
      await page.locator(`[data-step="${step}"]`).click();
      assert.equal(
        await page
          .locator(`[data-step="${step}"]`)
          .getAttribute("aria-pressed"),
        "true",
      );
    }
    for (const key of ["challenger", "evaluation", "allocation", "quarantine"])
      await page.locator(`[data-specialist="${key}"]`).click();
    for (const key of ["meridian", "research", "evaluation", "execution"])
      await page.locator(`[data-node="${key}"]`).click();
    for (const summary of await page.locator("summary").all()) {
      await summary.click();
      await summary.click();
    }
    const raw = execFileSync(
      process.env.PYTHON ?? "python",
      [
        "-m",
        "noema.cli",
        "economics-report",
        "--db",
        "/tmp/noema-public-qa-missing.db",
      ],
      { encoding: "utf8" },
    );
    await page.getByLabel("Import economics report").setInputFiles({
      name: "report.json",
      mimeType: "application/json",
      buffer: Buffer.from(raw),
    });
    await page
      .getByText("Imported snapshot · no recorded cash", { exact: true })
      .waitFor();
    assert.equal(await page.locator("#cash").innerText(), "0 USD");
    const bad = JSON.parse(raw);
    bad.self_funding_demonstrated = true;
    await page.getByLabel("Import economics report").setInputFiles({
      name: "bad.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(bad)),
    });
    await page.getByRole("alert").waitFor();
    assert.equal(await page.locator("#cash").innerText(), "0 USD");
    const hostile = JSON.parse(raw);
    hostile.gaps = ['<img src=x onerror="window.injected=true">'];
    await page.getByLabel("Import economics report").setInputFiles({
      name: "text.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(hostile)),
    });
    await page
      .locator("#report-gaps")
      .getByText("<img", { exact: false })
      .waitFor();
    assert.equal(await page.locator("#report-gaps img").count(), 0);
    await page.getByRole("button", { name: "Clear report" }).click();
    assert.equal(await page.locator("#cash").innerText(), "Unknown");
    await page.reload({ waitUntil: "networkidle" });
    assert.equal(
      await page.locator("#report-source").innerText(),
      "No report loaded",
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      `overflow at ${width}`,
    );
    for (const img of await page.locator("img").all())
      assert.ok(await img.evaluate((el) => el.complete && el.naturalWidth > 0));
    await page.locator(".skip").focus();
    await page.keyboard.press("Enter");
    assert.equal(new URL(page.url()).hash, "#main");
    await page.goto(url, { waitUntil: "networkidle" });
    if (process.env.QA_SCREENSHOTS) {
      await mkdir(process.env.QA_SCREENSHOTS, { recursive: true });
      await page.screenshot({
        path: `${process.env.QA_SCREENSHOTS}/noema-${width}.png`,
      });
    }
    await page.close();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(failed, []);
  assert.deepEqual(external, []);
  console.log(
    JSON.stringify({
      viewports: [390, 768, 1024, 1440],
      reportRoundtrip: "Python → browser passed",
      invalidReport: "rejected, previous snapshot retained",
      untrustedText: "inert",
      hashReload: "passed",
      keyboard: "passed",
      overflow: false,
      consoleErrors: errors,
      failedRequests: failed,
      externalRequests: external,
    }),
  );
} finally {
  await browser.close();
  server.close();
}
