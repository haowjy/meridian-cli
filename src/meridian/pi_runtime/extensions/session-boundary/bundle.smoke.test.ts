import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(dirname(fileURLToPath(import.meta.url)), "../../dist/extensions/session-boundary/index.js");

describe("session-boundary bundle", () => {
  it("requires a freshly built observer bundle with the native shutdown hook", () => {
    expect(existsSync(bundlePath), "run npm run build:extensions:session-boundary before bundle verification").toBe(true);
    const bundle = readFileSync(bundlePath, "utf8");
    expect(bundle).toContain("session_shutdown");
    expect(bundle).toContain("quit_candidate");
    expect(bundle).not.toContain("console.log");
  });
});
