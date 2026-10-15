import { expect, test } from "vitest";

import { classifyWorkId, isExactRunId, isStableSpawnId, parseRef } from "./ids";

test.each([
  ["p5", "spawn", "p5", undefined, 1],
  [" p5 ", "spawn", "p5", undefined, 1],
  ["p05", "spawn", "p05", undefined, 1],
  ["p5.1", "run", "p5", "p5", 1],
  ["p5.01", "run", "p5", "p5", 1],
  ["p5.2", "run", "p5", "p5.2", 2],
  ["p5.010", "run", "p5", "p5.10", 10],
  ["c05", "chat", undefined, undefined, undefined],
  ...["P5", "p-x", "r5", "p5.0", "p5.-1", "p５", "p5.2.3", "", "native-uuid"]
    .map(value => [value, "native", undefined, undefined, undefined]),
])("reference corpus %s", (text, kind, spawnId, runId, run) => {
  const ref = parseRef(text!);
  expect([ref.kind, ref.spawnId, ref.runId, ref.run]).toEqual([kind, spawnId, runId, run]);
});

test("returns all identity fields for chat and trimmed native refs", () => {
  expect(parseRef(" c05 ")).toEqual({ kind: "chat", value: "c05", chatId: "c05" });
  expect(parseRef(" native-id ")).toEqual({ kind: "native", value: "native-id" });
});

test("distinguishes stable spawn IDs from explicit run IDs", () => {
  expect(isStableSpawnId("p5")).toBe(true);
  expect(isStableSpawnId("p5.2")).toBe(false);
  expect(isExactRunId("p5")).toBe(false);
  expect(isExactRunId("p5.2")).toBe(true);
  expect(classifyWorkId("p5")).toBe("spawn");
  expect(classifyWorkId("p5.2")).toBe("spawn");
});
