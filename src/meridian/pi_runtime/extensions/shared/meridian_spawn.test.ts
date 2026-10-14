import { describe, expect, it, test } from "vitest";
import { isSpawnLauncherCommand, isSpawnWaitCommand, parseRef, extractSpawnIdFromLauncherLog, extractMeridianSpawnIdsFromText } from "./meridian_spawn";

test.each([
  ["p5", "spawn", "p5"], [" p5 ", "spawn", "p5"], ["p05", "spawn", "p05"],
  ["p5.1", "run", "p5"], ["p5.01", "run", "p5"], ["p5.2", "run", "p5.2"],
  ["p5.010", "run", "p5.10"], ["c05", "chat", undefined],
  ...["P5", "p-x", "r5", "p5.0", "p5.-1", "p５", "p5.2.3", "", "native-uuid"].map(value => [value, "native", undefined]),
])("reference corpus %s", (text, kind, runId) => {
  expect(parseRef(text!)).toMatchObject({ kind });
  expect(parseRef(text!).runId).toBe(runId);
});

test("launcher speaks spawn; observations keep exact run keys", () => {
  expect(extractSpawnIdFromLauncherLog("Spawn id: p5")).toBe("p5");
  expect(extractSpawnIdFromLauncherLog("Spawn id: p5.2")).toBe("p5");
  expect(extractMeridianSpawnIdsFromText("p5.2 and p6 and p5.2")).toEqual(["p5.2", "p6"]);
});

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
