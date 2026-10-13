// Copy the rendered video and poster into the site (site/assets/media/), refusing files over budget.
import { copyFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "..", "out");
const media = join(here, "..", "..", "site", "assets", "media");
const budgets = { "mac-mcp-demo.mp4": 4_000_000, "demo-poster.jpg": 150_000 };

for (const [name, limit] of Object.entries(budgets)) {
  const size = statSync(join(out, name)).size;
  if (size > limit) {
    console.error(`${name} is ${size} bytes, over its ${limit} byte budget`);
    process.exit(1);
  }
  copyFileSync(join(out, name), join(media, name));
  console.log(`${name}: ${(size / 1000).toFixed(0)} KB → site/assets/media/`);
}
