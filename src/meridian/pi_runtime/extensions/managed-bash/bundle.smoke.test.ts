import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/managed-bash/index.js",
);

const bundleExists = existsSync(bundlePath);

describe.skipIf(!bundleExists)("managed-bash bundle smoke", () => {
  it("registers the bash and bash_manage tools", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).toContain('name: "bash"');
    expect(src).toContain('name: "bash_manage"');
    expect(src).toContain('registerCommand("ps:clear"');
  });

  it("does not import pi-tui subpaths that break Pi extension aliasing", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).not.toMatch(/@earendil-works\/pi-tui\/dist\//);
  });
});
