import { existsSync, readFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

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
    expect(src).toContain("notificationAdmission");
    expect(src).toContain("requestAdmittedScan");
    expect(src).toContain("triggerTurn: true");
    expect(src).not.toContain('registerCommand("mspawn"');
    expect(src).not.toContain("registerTool");
  });

  it("does not import pi-tui subpaths that break Pi extension aliasing", () => {
    const src = readFileSync(bundlePath, "utf8");
    expect(src).not.toMatch(/@earendil-works\/pi-tui\/dist\//);
  });

  it("loads the source-built entrypoint and registers admission lifecycle hooks", async () => {
    const stateDir = await mkdtemp(join(tmpdir(), "pi-spawn-watch-bundle-"));
    process.env._MERIDIAN_PI_STATE_DIR = stateDir;
    process.env.MERIDIAN_SPAWN_ID = "p-bundle-test";
    process.env._MERIDIAN_PI_NOTIFICATION_GATE_VERSION = "1";
    process.env._MERIDIAN_PI_NOTIFICATION_GATE_ATTEMPT = "attempt-bundle-test";
    process.env._MERIDIAN_PI_NOTIFICATION_GATE_NONCE = `bundle-${Math.random()}`;
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    try {
      const extension = (await import("../../dist/extensions/meridian-spawn-watch/index.js")).default;
      extension({
        on: (name: string, handler: (...args: unknown[]) => unknown) => { handlers.set(name, handler); },
        registerCommand: () => undefined,
      } as never);
      expect(handlers.has("agent_start")).toBe(true);
      expect(handlers.has("session_before_switch")).toBe(true);
      handlers.get("session_start")?.({ reason: "startup" });
      handlers.get("session_shutdown")?.({ reason: "quit" });
    } finally {
      for (const key of ["_MERIDIAN_PI_STATE_DIR", "MERIDIAN_SPAWN_ID", "_MERIDIAN_PI_NOTIFICATION_GATE_VERSION", "_MERIDIAN_PI_NOTIFICATION_GATE_ATTEMPT", "_MERIDIAN_PI_NOTIFICATION_GATE_NONCE"]) {
        delete process.env[key];
      }
      await rm(stateDir, { recursive: true, force: true });
    }
  });
});
