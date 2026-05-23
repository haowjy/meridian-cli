import { describe, expect, it } from "vitest";

import { computePanelLayout } from "./layout";

describe("computePanelLayout", () => {
  it("does not reserve eight process rows for a single task", () => {
    const layout = computePanelLayout(40, 1);
    expect(layout.maxVisibleProcesses).toBe(1);
    expect(layout.maxPreviewLines).toBeGreaterThan(3);
  });

  it("allocates more log space on tall terminals", () => {
    const short = computePanelLayout(24, 2);
    const tall = computePanelLayout(50, 2);
    expect(tall.maxPreviewLines).toBeGreaterThan(short.maxPreviewLines);
  });
});
