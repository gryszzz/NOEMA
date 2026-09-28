import { cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("../", import.meta.url));
const base = process.env.SITE_BASE_PATH ?? "/NOEMA/";
if (!/^\/(?:[A-Za-z0-9_-]+\/)*$/.test(base))
  throw new Error(
    "SITE_BASE_PATH must be an absolute directory path with a trailing slash.",
  );
const files = await readdir(root + "site");
const html = await readFile(root + "site/index.html", "utf8");
for (const [, asset] of html.matchAll(/(?:href|src)="\.\/([^"]+)"/g))
  if (!files.includes(asset)) throw new Error("Missing site asset: " + asset);
for (const [, id] of html.matchAll(/href="#([^"]+)"/g))
  if (!html.includes(`id="${id}"`)) throw new Error("Missing anchor: " + id);
await rm(root + "dist-site", { recursive: true, force: true });
await mkdir(root + "dist-site");
for (const name of files)
  if (!name.endsWith(".test.mjs"))
    await cp(root + "site/" + name, root + "dist-site/" + name);
await writeFile(
  root + "dist-site/index.html",
  html.replaceAll(/((?:href|src)=")\.\//g, "$1" + base),
);
await writeFile(root + "dist-site/.nojekyll", "");
console.log(
  `Built NOEMA public site at ${base}; local assets and hash routes validated.`,
);
