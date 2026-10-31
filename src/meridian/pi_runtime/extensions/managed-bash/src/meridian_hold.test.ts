import { describe, expect, it } from "vitest";
import { keepsForeground } from "./meridian_hold";

describe("keepsForeground", () => {
  it("re-arms a held task without timeout_min", () => expect(keepsForeground(true, false, 10 * 60_000)).toBe(true));
  it("backgrounds a held task after 55 minutes", () => expect(keepsForeground(true, false, 55 * 60_000)).toBe(false));
  it("backgrounds on an explicit timeout_min", () => expect(keepsForeground(true, true, 1)).toBe(false));
  it("backgrounds a task that is not held", () => expect(keepsForeground(false, false, 1)).toBe(false));
});
