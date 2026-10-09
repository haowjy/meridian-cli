import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/meridian-idle/index.js",
);

const bundleExists = existsSync(bundlePath);

describe.skipIf(!bundleExists)("meridian-idle bundle smoke", () => {
  it("registers only the idle lifecycle sensors", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).toContain('pi.on("session_start"');
    expect(src).toContain('pi.on("agent_end"');
    expect(src).toContain('pi.on("input"');
    expect(src).not.toContain("registerTool");
    expect(src).not.toContain("registerCommand");
  });
});
