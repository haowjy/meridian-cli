import { describe, expect, it } from "vitest";

import { computePanelLayout } from "./layout";

describe("computePanelLayout", () => {
  it("uses one process row for a single task", () => {
    const layout = computePanelLayout(40, 1);
    expect(layout.maxVisibleProcesses).toBe(1);
    expect(layout.maxPreviewLines).toBe(40 - 11 - 1);
  });

  it("expands process rows up to eight tasks", () => {
    expect(computePanelLayout(40, 3).maxVisibleProcesses).toBe(3);
    expect(computePanelLayout(40, 12).maxVisibleProcesses).toBe(8);
  });

  it("gives more log space when fewer processes", () => {
    const one = computePanelLayout(40, 1);
    const eight = computePanelLayout(40, 8);
    expect(one.maxPreviewLines).toBeGreaterThan(eight.maxPreviewLines);
  });
});
