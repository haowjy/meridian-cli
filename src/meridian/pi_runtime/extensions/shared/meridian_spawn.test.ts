import { describe, expect, it } from "vitest";

import {
  extractSpawnIdFromLauncherLog,
  isMeridianSpawnCommand,
  isSpawnLauncherCommand,
  isSpawnWaitCommand,
} from "./meridian_spawn";

describe("spawn command classification", () => {
  it("treats uv run meridian spawn as launcher", () => {
    expect(isSpawnLauncherCommand("uv run meridian spawn -m gptmini")).toBe(true);
    expect(isSpawnWaitCommand("uv run meridian spawn -m gptmini")).toBe(false);
  });

  it("treats env-prefixed meridian spawn as launcher", () => {
    expect(isSpawnLauncherCommand("FOO=1 meridian spawn -m x")).toBe(true);
  });

  it("treats meridian spawn wait as wait, not launcher", () => {
    expect(isSpawnWaitCommand("uv run meridian spawn wait")).toBe(true);
    expect(isSpawnLauncherCommand("uv run meridian spawn wait")).toBe(false);
    expect(isSpawnWaitCommand("FOO=1 meridian spawn wait p1 p2")).toBe(true);
  });

  it("extracts spawn id from launcher log note", () => {
    const log = "Background spawn submitted.\nSpawn id: p2538\n";
    expect(extractSpawnIdFromLauncherLog(log)).toBe("p2538");
    expect(extractSpawnIdFromLauncherLog("launched p2538\n")).toBe(null);
  });
});

describe("isMeridianSpawnCommand", () => {
  it("matches any command that invokes meridian spawn", () => {
    for (const command of [
      "meridian spawn wait",
      "cd /x && meridian spawn wait p1",
      "meridian spawn wait 2>&1 | tail -50",
      "uv run meridian spawn wait",
      "/usr/local/bin/meridian spawn wait",
      "meridian --json spawn wait",
      "meridian -C /tmp/x spawn wait",
      "meridian --format json spawn wait",
      "meridian --directory=/tmp spawn wait",
    ]) expect(isMeridianSpawnCommand(command), command).toBe(true);
  });

  it("rejects commands without one", () => {
    for (const command of ["sleep 270", "echo meridian", "meridianx spawn wait", "meridian work show", "meridian work; spawn", ""]) {
      expect(isMeridianSpawnCommand(command), command).toBe(false);
    }
  });

  it("keeps wait detection through global options", () => {
    expect(isSpawnWaitCommand("meridian -C /x spawn wait p1")).toBe(true);
    expect(isSpawnLauncherCommand("meridian -C /x spawn wait p1")).toBe(false);
    expect(isSpawnLauncherCommand("meridian -C /x spawn -m foo")).toBe(true);
  });
});
