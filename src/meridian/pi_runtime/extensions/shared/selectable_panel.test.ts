import type { Theme } from "@earendil-works/pi-coding-agent";
import { describe, expect, it } from "vitest";

import { renderSelectablePanel, type SelectablePanelOptions } from "./selectable_panel";

type Row = { id: string; status: string; command: string };

function mockTheme(): Theme {
  const pass = (value: string): string => value;
  return { fg: (_role: string, value: string) => value, bold: pass } as unknown as Theme;
}

const rows: Row[] = [
  { id: "b-1", status: "running", command: "sleep 1" },
  { id: "b-2", status: "exited", command: "echo done" },
];

const options: SelectablePanelOptions<Row> = {
  title: "Meridian /ps",
  columns: [
    { header: "ID", width: 8, render: (row) => row.id },
    { header: "STATE", width: 10, render: (row) => row.status },
    { header: "COMMAND", width: 20, render: (row) => row.command },
  ],
  loadRows: async () => rows,
  getRowId: (row) => row.id,
  renderPreview: (row) => [`preview ${row.id}`],
  footer: "enter logs · j/k select · r refresh · q close",
};

describe("renderSelectablePanel", () => {
  it("renders shared title, selected row, preview, and footer", () => {
    const lines = renderSelectablePanel(80, 24, mockTheme(), options, rows, 1, 0);

    expect(lines.join("\n")).toContain("Meridian /ps");
    expect(lines.join("\n")).toContain("preview b-2");
    expect(lines.join("\n")).toContain("> b-2");
    expect(lines.join("\n")).toContain("enter logs · j/k select · r refresh · q close");
  });

  it("renders an empty message when no rows exist", () => {
    const lines = renderSelectablePanel(
      80,
      24,
      mockTheme(),
      { ...options, emptyMessage: "Nothing here" },
      [],
      0,
      0,
    );

    expect(lines.join("\n")).toContain("Nothing here");
  });
});
