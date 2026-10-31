import { describe, expect, it } from "vitest";

import {
  extractSpawnIdFromLauncherLog,
  isSingleMeridianSpawnCommand,
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

describe("isSingleMeridianSpawnCommand", () => {
  it("matches one spawn invocation, with optional global options", () => {
    for (const command of [
      "meridian spawn wait",
      "meridian spawn wait p12 p13",
      "meridian spawn -a coder -p 'hi; there'",
      "/usr/local/bin/meridian spawn wait",
      "meridian -C /tmp/x spawn wait",
      "meridian --format json spawn wait",
      "meridian --directory=/tmp spawn wait",
    ]) expect(isSingleMeridianSpawnCommand(command), command).toBe(true);
  });

  it("rejects anything else", () => {
    for (const command of [
      "sleep 270",
      "meridian work show",
      "meridian spawn wait && echo x",
      "meridian spawn wait; ls",
      "meridian spawn wait | cat",
      "echo meridian spawn wait",
      "meridian $(echo spawn) wait",
      "cd x\nmeridian spawn wait",
      "meridian 'spawn' wait",
      "meridian --bogus spawn wait",
      "",
    ]) expect(isSingleMeridianSpawnCommand(command), command).toBe(false);
  });
});
