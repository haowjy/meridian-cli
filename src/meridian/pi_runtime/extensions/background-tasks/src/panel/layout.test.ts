import { describe, expect, it } from "vitest";

import { computePanelLayout, MAX_LOG_PREVIEW_LINES } from "./layout";

describe("computePanelLayout", () => {
  it("uses one process row for a single task", () => {
    expect(computePanelLayout(40, 1).maxVisibleProcesses).toBe(1);
  });

  it("expands process rows up to eight tasks", () => {
    expect(computePanelLayout(40, 3).maxVisibleProcesses).toBe(3);
    expect(computePanelLayout(40, 12).maxVisibleProcesses).toBe(8);
  });

  it("uses a fixed four-line log preview", () => {
    expect(computePanelLayout(24, 1).maxPreviewLines).toBe(MAX_LOG_PREVIEW_LINES);
    expect(computePanelLayout(50, 8).maxPreviewLines).toBe(MAX_LOG_PREVIEW_LINES);
  });
});
