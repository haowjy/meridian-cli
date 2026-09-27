import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, onTestFinished } from "vitest";

const fixturePath = fileURLToPath(new URL("./lifecycle.fixture.mjs", import.meta.url));

describe("session-boundary built bundle registration", () => {
  it.each(["quit", "restart", "eof", "exit", "eof-race"])("publishes %s through Pi lifecycle callbacks", (shape) => {
    const directory = mkdtempSync(join(tmpdir(), "boundary-bundle-"));
    onTestFinished(() => rmSync(directory, { recursive: true, force: true }));
    const recordPath = join(directory, "record.json");
    const child = spawnSync(process.execPath, [fixturePath, shape], {
      env: {
        ...process.env,
        _MERIDIAN_PI_SESSION_BOUNDARY_PATH: recordPath,
        _MERIDIAN_PI_SESSION_BOUNDARY_NONCE: "bundle-test-nonce",
      },
      encoding: "utf8",
      timeout: 5000,
    });
    expect(child.error).toBeUndefined();
    expect(child.signal).toBeNull();
    expect(child.stderr).toBe("");
    expect(child.stdout).toBe("");
    expect(child.status).toBe(0);
    expect(JSON.parse(readFileSync(recordPath, "utf8"))).toMatchObject({
      v: 2, launch_nonce: "bundle-test-nonce", pid: child.pid,
      revision: shape === "restart" ? 6 : ["quit", "eof"].includes(shape) ? 5 : 4,
      initial: { session_id: "native-entry", session_file: "/native-store/1_native-entry.jsonl" },
      current: shape === "eof-race"
        ? { session_id: "native-entry", session_file: "/native-store/1_native-entry.jsonl" }
        : { session_id: "native-exit", session_file: "/native-store/2_native-exit.jsonl" },
      quit: ["quit", "eof"].includes(shape)
        ? { session_id: "native-exit", session_file: "/native-store/2_native-exit.jsonl" } : null,
      last_event: ["quit", "eof", "eof-race"].includes(shape)
        ? { type: "session_shutdown", reason: "quit" } : { type: "session_start", reason: "new" },
      invalid_reason: null,
    });
  });
});
