import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/meridian-idle/index.js",
);

describe.skipIf(!existsSync(bundlePath))("meridian-idle bundle smoke", () => {
  it("loads and registers only the idle lifecycle sensors", async () => {
    const bundle = await import(/* @vite-ignore */ pathToFileURL(bundlePath).href);
    const events: string[] = [];

    bundle.default({ on: (name: string) => events.push(name) });

    expect(events).toEqual(["session_start", "agent_end", "input", "session_shutdown"]);
  });
});
