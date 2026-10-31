import { describe, expect, it } from "vitest";

import {
  extractSpawnIdFromLauncherLog,
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
