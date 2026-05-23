import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/background-tasks/index.js",
);

const bundleExists = existsSync(bundlePath);

describe.skipIf(!bundleExists)("background-tasks bundle smoke", () => {
  it("defines isPanelQuit before TasksPanelComponent (avoids free-global handleInput)", () => {
    const src = readFileSync(bundlePath, "utf8");
    const fnPos = src.indexOf("function isPanelQuit");
    const classPos = src.indexOf("var TasksPanelComponent = class");
    expect(fnPos).toBeGreaterThan(-1);
    expect(classPos).toBeGreaterThan(-1);
    expect(fnPos).toBeLessThan(classPos);

    const handlePos = src.indexOf("handleInput(data)", classPos);
    expect(handlePos).toBeGreaterThan(-1);
    const snippet = src.slice(handlePos, handlePos + 200);
    expect(snippet).toContain("isPanelQuit(data)");
  });

  it("does not import pi-tui subpaths that break Pi extension aliasing", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).not.toMatch(/@earendil-works\/pi-tui\/dist\//);
  });
});
