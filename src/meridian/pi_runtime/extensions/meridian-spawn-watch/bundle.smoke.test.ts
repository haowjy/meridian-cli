import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/meridian-spawn-watch/index.js",
);

const bundleExists = existsSync(bundlePath);

describe.skipIf(!bundleExists)("meridian-spawn-watch bundle smoke", () => {
  it("registers spawn commands without tools", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).toContain('registerCommand("spawn"');
    expect(src).toContain('registerCommand("spawn:clear"');
    expect(src).not.toContain('registerCommand("mspawn"');
    expect(src).not.toContain("registerTool");
  });

  it("does not import pi-tui subpaths that break Pi extension aliasing", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).not.toMatch(/@earendil-works\/pi-tui\/dist\//);
  });
});
