import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/meridian-permissions/index.js",
);

afterEach(() => {
  delete process.env._MERIDIAN_TOOL_POLICY;
  delete process.env._MERIDIAN_TOOL_EVALUATOR;
});

describe.skipIf(!existsSync(bundlePath))("meridian-permissions bundle smoke", () => {
  it("registers only the policy hooks", async () => {
    process.env._MERIDIAN_TOOL_POLICY = "{}";
    process.env._MERIDIAN_TOOL_EVALUATOR = JSON.stringify(["python3", "evaluate.py"]);
    const bundle = await import(/* @vite-ignore */ pathToFileURL(bundlePath).href);
    const events: string[] = [];

    bundle.default({ on: (name: string) => events.push(name) });

    expect(events).toEqual(["tool_call"]);
  });

  it("throws instead of loading without a policy", async () => {
    process.env._MERIDIAN_TOOL_POLICY = "";
    const bundle = await import(/* @vite-ignore */ pathToFileURL(bundlePath).href);

    expect(() => bundle.default({ on: () => undefined })).toThrow(
      "Pi permission policy is missing",
    );
  });
});
