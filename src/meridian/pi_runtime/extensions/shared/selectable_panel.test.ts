import type { ExtensionContext, Theme } from "@earendil-works/pi-coding-agent";
import { describe, expect, it, vi } from "vitest";

import {
  openTaskPanel,
  hasTaskPanelUI,
  renderSelectablePanel,
  SelectablePanelComponent,
  type PanelCommandContext,
  type PanelDetail,
  type SelectablePanelOptions,
} from "./selectable_panel";

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

describe("openTaskPanel", () => {
  it("keeps native TUI panels available while routing RPC to notifications", () => {
    expect(hasTaskPanelUI({ hasUI: true, mode: "tui" } as unknown as ExtensionContext)).toBe(true);
    expect(hasTaskPanelUI({ hasUI: true, mode: "rpc" } as unknown as ExtensionContext)).toBe(false);
    expect(hasTaskPanelUI({ hasUI: true } as ExtensionContext)).toBe(true);
  });

  it("opens task panels as full-screen covering overlays", async () => {
    let receivedOptions: Record<string, unknown> | undefined;
    const ctx: PanelCommandContext = {
      ui: {
        custom: async (_factory, options) => {
          receivedOptions = options;
          return undefined;
        },
      },
    };

    await openTaskPanel(ctx, options);

    expect(receivedOptions).toEqual({
      overlay: true,
      overlayOptions: {
        width: "100%",
        maxHeight: "100%",
        anchor: "top-left",
      },
    });
  });
});

describe("SelectablePanelComponent", () => {
  it("shows a failed clear action while retaining the existing rows", async () => {
    const component = new SelectablePanelComponent(
      { requestRender: () => undefined, terminal: { rows: 24 } }, mockTheme(),
      { ...options, onClear: async () => { throw new Error("invalid delivery receipts"); } },
      () => undefined,
    );
    await vi.waitFor(() => expect(component.render(80).join("\n")).toContain("b-1"));
    component.handleInput("c");
    await vi.waitFor(() => {
      const rendered = component.render(80).join("\n");
      expect(rendered).toContain("Action failed: invalid delivery receipts");
      expect(rendered).toContain("b-1");
    });
  });

  it("runs the clear action from c", async () => {
    let cleared = 0;
    const component = new SelectablePanelComponent(
      { requestRender: () => undefined, terminal: { rows: 24 } },
      mockTheme(),
      { ...options, onClear: async () => { cleared += 1; } },
      () => undefined,
    );

    component.handleInput("c");
    await Promise.resolve();

    expect(cleared).toBe(1);
  });
});

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: Error) => void };
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const settle = async (): Promise<void> => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };

describe("SelectablePanelComponent lazy detail", () => {
  const threeRows: Row[] = [
    { id: "b-1", status: "running", command: "one" },
    { id: "b-2", status: "exited", command: "two" },
    { id: "b-3", status: "exited", command: "three" },
  ];

  function detailOptions(
    loadDetail: (row: Row) => Promise<string>,
    overrides: Partial<SelectablePanelOptions<Row, string>> = {},
  ): SelectablePanelOptions<Row, string> {
    const describeDetail = (detail?: PanelDetail<string>): string =>
      !detail ? "none" : detail.status === "ready" ? `ready:${detail.value}` : detail.status === "error" ? `error:${detail.message}` : "loading";
    return {
      ...options,
      loadRows: async () => threeRows,
      loadDetail,
      renderPreview: (row, _theme, detail) => [`preview ${row.id} ${describeDetail(detail)}`],
      ...overrides,
    };
  }

  function open(panelOptions: SelectablePanelOptions<Row, string>) {
    const requestRender = vi.fn();
    let done = 0;
    const component = new SelectablePanelComponent<Row, string>(
      { requestRender, terminal: { rows: 24 } }, mockTheme(), panelOptions, () => { done += 1; },
    );
    return { component, requestRender, text: () => component.render(100).join("\n"), doneCount: () => done };
  }

  it("reads detail for the selected row only, never for the other listed rows", async () => {
    const reads: string[] = [];
    const panel = open(detailOptions(async (row) => { reads.push(row.id); return `log ${row.id}`; }));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready:log b-1"));
    expect(panel.text()).toContain("b-3");
    expect(reads).toEqual(["b-1"]);
  });

  it("keeps one read in flight and coalesces rapid selection to the latest row", async () => {
    const held: Record<string, Deferred<string>> = {};
    const reads: string[] = [];
    let active = 0;
    let maxActive = 0;
    const panel = open(detailOptions(async (row) => {
      reads.push(row.id);
      active += 1;
      maxActive = Math.max(maxActive, active);
      held[row.id] = deferred<string>();
      try { return await held[row.id]!.promise; } finally { active -= 1; }
    }));
    await vi.waitFor(() => expect(reads).toEqual(["b-1"]));

    panel.component.handleInput("j");
    panel.component.handleInput("j");
    await settle();
    expect(reads).toEqual(["b-1"]);
    expect(panel.text()).toContain("preview b-3 loading");

    held["b-1"]!.resolve("old-selection");
    await vi.waitFor(() => expect(reads).toEqual(["b-1", "b-3"]));
    expect(panel.text()).not.toContain("old-selection");
    expect(panel.text()).toContain("preview b-3 loading");

    held["b-3"]!.resolve("latest");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-3 ready:latest"));
    expect(reads).toEqual(["b-1", "b-3"]);
    expect(maxActive).toBe(1);
  });

  it("keeps the loaded row's detail when selection returns to it while another read is held", async () => {
    const held: Record<string, Deferred<string>> = {};
    const reads: string[] = [];
    const panel = open(detailOptions((row) => { reads.push(row.id); held[row.id] = deferred<string>(); return held[row.id]!.promise; }));
    await vi.waitFor(() => expect(reads).toEqual(["b-1"]));
    held["b-1"]!.resolve("one-log");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready:one-log"));

    panel.component.handleInput("j");
    await vi.waitFor(() => expect(reads).toEqual(["b-1", "b-2"]));
    panel.component.handleInput("k");
    expect(panel.text()).toContain("preview b-1 ready:one-log");

    held["b-2"]!.resolve("two-log");
    await settle();
    expect(panel.text()).toContain("preview b-1 ready:one-log");
    expect(reads).toEqual(["b-1", "b-2"]);
  });

  it("never shows a stale read's error on a different selection", async () => {
    const held: Record<string, Deferred<string>> = {};
    const reads: string[] = [];
    const panel = open(detailOptions((row) => { reads.push(row.id); held[row.id] = deferred<string>(); return held[row.id]!.promise; }));
    await vi.waitFor(() => expect(reads).toEqual(["b-1"]));
    panel.component.handleInput("j");
    held["b-1"]!.reject(new Error("EIO on b-1"));
    await vi.waitFor(() => expect(reads).toEqual(["b-1", "b-2"]));
    expect(panel.text()).not.toContain("EIO on b-1");
    held["b-2"]!.resolve("two-log");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-2 ready:two-log"));
  });

  it("shows a read failure for the row that failed and recovers on the next selection", async () => {
    const panel = open(detailOptions(async (row) => {
      if (row.id === "b-1") throw new Error("EACCES b-1");
      return `log ${row.id}`;
    }));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 error:EACCES b-1"));
    expect(panel.text()).toContain("b-2");
    panel.component.handleInput("j");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-2 ready:log b-2"));
  });

  it("reads fresh detail for the same selected row after a refresh, even over a held older read", async () => {
    let generation = 0;
    const held: Deferred<string>[] = [];
    const reads: string[] = [];
    const panel = open(detailOptions(
      (row) => { reads.push(row.id); held.push(deferred<string>()); return held.at(-1)!.promise; },
      { loadRows: async () => { generation += 1; return threeRows; } },
    ));
    await vi.waitFor(() => expect(reads).toEqual(["b-1"]));

    panel.component.handleInput("r");
    await vi.waitFor(() => expect(generation).toBe(2));
    await settle();
    held[0]!.resolve("before-refresh");
    await vi.waitFor(() => expect(reads).toEqual(["b-1", "b-1"]));
    expect(panel.text()).not.toContain("before-refresh");
    held[1]!.resolve("after-refresh");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready:after-refresh"));

    panel.component.handleInput("r");
    await vi.waitFor(() => expect(reads).toHaveLength(3));
    expect(panel.text()).toContain("preview b-1 ready:after-refresh");
    held[2]!.resolve("third");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready:third"));
  });

  it("keeps the selected row identity across reorder and removal", async () => {
    let current = threeRows;
    const reads: string[] = [];
    const panel = open(detailOptions(async (row) => { reads.push(row.id); return `log ${row.id}`; }, { loadRows: async () => current }));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready"));
    panel.component.handleInput("j");
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-2 ready"));

    current = [threeRows[2]!, threeRows[1]!, threeRows[0]!];
    panel.component.handleInput("r");
    await vi.waitFor(() => expect(reads).toEqual(["b-1", "b-2", "b-2"]));
    expect(panel.text()).toContain("> b-2");
    expect(panel.text()).toContain("preview b-2 ready:log b-2");

    current = [threeRows[2]!, threeRows[0]!];
    panel.component.handleInput("r");
    await vi.waitFor(() => expect(panel.text()).toContain("> b-1"));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready:log b-1"));
    expect(panel.text()).not.toContain("b-2 ");
  });

  it("applies a selection made while rows were loading", async () => {
    const pending = deferred<Row[]>();
    let loads = 0;
    const panel = open(detailOptions(async (row) => `log ${row.id}`, {
      loadRows: () => (++loads === 1 ? Promise.resolve(threeRows) : pending.promise),
    }));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready"));
    panel.component.handleInput("r");
    panel.component.handleInput("j");
    pending.resolve(threeRows);
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-2 ready:log b-2"));
    expect(panel.text()).toContain("> b-2");
  });

  it("renders nothing and reads nothing once closed, even when a held read settles later", async () => {
    const held = deferred<string>();
    const reads: string[] = [];
    const panel = open(detailOptions((row) => { reads.push(row.id); return held.promise; }));
    await vi.waitFor(() => expect(reads).toEqual(["b-1"]));
    panel.component.handleInput("j");
    panel.component.handleInput("q");
    expect(panel.doneCount()).toBe(1);
    panel.requestRender.mockClear();

    held.resolve("late");
    await settle();
    expect(panel.requestRender).not.toHaveBeenCalled();
    expect(reads).toEqual(["b-1"]);
  });

  it("stops on host disposal and does not render a late row load or action failure", async () => {
    const pendingRows = deferred<Row[]>();
    const pendingClear = deferred<void>();
    let loads = 0;
    const panel = open(detailOptions(async (row) => `log ${row.id}`, {
      loadRows: () => (++loads === 1 ? Promise.resolve(threeRows) : pendingRows.promise),
      onClear: () => pendingClear.promise,
    }));
    await vi.waitFor(() => expect(panel.text()).toContain("preview b-1 ready"));
    panel.component.handleInput("r");
    panel.component.handleInput("c");
    panel.component.dispose();
    panel.requestRender.mockClear();

    pendingRows.resolve(threeRows);
    pendingClear.reject(new Error("late failure"));
    await settle();
    expect(panel.requestRender).not.toHaveBeenCalled();
    expect(panel.text()).not.toContain("late failure");
  });

  it("leaves synchronous previews untouched when a panel supplies no detail loader", () => {
    const renderPreview = vi.fn((row: Row) => [`preview ${row.id}`]);
    const lines = renderSelectablePanel(80, 24, mockTheme(), { ...options, renderPreview }, rows, 0, 0);
    expect(lines.join("\n")).toContain("preview b-1");
    expect(renderPreview).toHaveBeenCalledWith(rows[0], expect.anything(), undefined);
  });
});

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
