import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

const bundlePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../dist/extensions/meridian-message/index.js",
);

describe.skipIf(!existsSync(bundlePath))("meridian-message bundle smoke", () => {
  it("registers nothing without a control socket path", async () => {
    const bundle = await import(/* @vite-ignore */ pathToFileURL(bundlePath).href);
    const events: string[] = [];

    bundle.default({ on: (name: string) => events.push(name) });

    expect(events).toEqual([]);
  });

  it("registers only the session lifecycle, agent_start and compaction hooks when a path is set", async () => {
    const bundle = await import(/* @vite-ignore */ pathToFileURL(bundlePath).href);
    const events: string[] = [];
    process.env._MERIDIAN_PI_CONTROL_SOCKET = "/tmp/never-bound.sock";

    try {
      bundle.default({ on: (name: string) => events.push(name) });
    } finally {
      delete process.env._MERIDIAN_PI_CONTROL_SOCKET;
    }

    expect(events).toEqual([
      "session_start",
      "agent_start",
      "session_before_compact",
      "session_compact",
      "session_compact_failed",
      "session_shutdown",
    ]);
  });
});
