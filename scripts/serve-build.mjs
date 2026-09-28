import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname, sep } from "node:path";
export async function serveBuild(directory, base) {
  const root = resolve(directory);
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://localhost");
      if (!url.pathname.startsWith(base)) {
        res.writeHead(404).end();
        return;
      }
      const path = resolve(
        root,
        decodeURIComponent(url.pathname.slice(base.length)) || "index.html",
      );
      if (!path.startsWith(root + sep)) {
        res.writeHead(404).end();
        return;
      }
      const body = await readFile(path);
      const types = {
        ".html": "text/html",
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".json": "application/json",
      };
      res
        .writeHead(200, {
          "Content-Type": types[extname(path)] ?? "application/octet-stream",
        })
        .end(body);
    } catch {
      res.writeHead(404).end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}${base}` };
}
