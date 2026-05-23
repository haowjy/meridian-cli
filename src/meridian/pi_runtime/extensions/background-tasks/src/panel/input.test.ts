import { describe, expect, it } from "vitest";

import { isPanelKill, isPanelQuit, printableChar } from "./input";

describe("panel input", () => {
  it("recognizes plain q", () => {
    expect(isPanelQuit("q")).toBe(true);
    expect(printableChar("q")).toBe("q");
  });

  it("recognizes kitty-encoded q", () => {
    const kittyQ = "\x1b[113u";
    expect(isPanelQuit(kittyQ)).toBe(true);
    expect(printableChar(kittyQ)).toBe("q");
  });

  it("recognizes kill key", () => {
    expect(isPanelKill("x")).toBe(true);
  });
});
