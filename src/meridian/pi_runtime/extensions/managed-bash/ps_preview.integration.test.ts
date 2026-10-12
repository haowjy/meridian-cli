import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

import { BashRuntime } from "./src/bash_runtime";
import { readLogTail } from "./src/bash_log_store";
import managedBashExtension from "./src/index";

// Spy on the reader imported by /ps only; the runtime's own log store keeps calling the real one internally.
vi.mock("./src/bash_log_store", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./src/bash_log_store")>();
  return { ...actual, readLogTail: vi.fn(actual.readLogTail) };
});

const previewReads = vi.mocked(readLogTail);
const theme = { fg: (_role: string, value: string) => value, bold: (value: string) => value };
const WORDS = ["alpha", "beta", "gamma"];
const outputOf = (row: { command: string }): string => `OUT-${/^w=(\w+);/.exec(row.command)![1]}`;

type PanelComponent = { render(width: number): string[]; handleInput(data: string): void };
type PsCommand = { handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> };

async function setup() {
  const root = await mkdtemp(path.join(tmpdir(), "pi-ps-preview-"));
  vi.stubEnv("_MERIDIAN_PI_STATE_DIR", root);
  vi.stubEnv("MERIDIAN_SPAWN_ID", "p900002");
  const runtime = new BashRuntime();
  // The command text never contains its own output, so output visibility proves a log was read.
  for (const word of WORDS) await runtime.execute({ command: `w=${word}; printf "OUT-$w"` }, undefined);
  const commands = new Map<string, PsCommand>();
  managedBashExtension({
    on: () => {}, registerTool: () => {},
    registerCommand: (name: string, command: PsCommand) => commands.set(name, command),
  } as unknown as ExtensionAPI);
  const rows = runtime.list(true);
  previewReads.mockClear();
  return { root, runtime, ps: commands.get("ps")!, rows };
}

/** Opens the real /ps panel against a fake TUI host and hands the live component to the test. */
function openPanel(ps: PsCommand) {
  let component!: PanelComponent;
  let mount!: () => void;
  const mounted = new Promise<void>((resolve) => { mount = resolve; });
  const ctx = {
    hasUI: true, mode: "tui",
    ui: {
      notify: vi.fn(),
      custom: (factory: (...args: unknown[]) => PanelComponent) => new Promise<void>((done) => {
        component = factory({ requestRender: vi.fn(), terminal: { rows: 30 } }, theme, undefined, () => done());
        mount();
      }),
    },
  } as unknown as ExtensionCommandContext;
  const closed = ps.handler("", ctx);
  return { mounted, closed, input: (key: string) => component.handleInput(key), text: () => component.render(120).join("\n") };
}

afterEach(() => { vi.unstubAllEnvs(); previewReads.mockReset(); });

describe("/ps task preview I/O", () => {
  it("lists tasks in non-TUI mode without reading any log", async () => {
    const { root, runtime, ps, rows } = await setup();
    try {
      const notify = vi.fn();
      const ctx = { hasUI: true, mode: "rpc", ui: { notify, custom: async () => undefined } } as unknown as ExtensionCommandContext;
      await ps.handler("", ctx);
      expect(previewReads).not.toHaveBeenCalled();
      for (const row of rows) expect(notify.mock.calls[0]![0]).toContain(row.bash_id);
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("lists every row in the TUI but reads only the selected row, one read at a time, latest selection wins", async () => {
    const { root, runtime, ps, rows } = await setup();
    try {
      const real = await vi.importActual<typeof import("./src/bash_log_store")>("./src/bash_log_store");
      let gate: Promise<void> | null = null;
      let inFlight = 0;
      let maxInFlight = 0;
      previewReads.mockImplementation(async (file, bytes) => {
        inFlight += 1; maxInFlight = Math.max(maxInFlight, inFlight);
        try { await gate; return await real.readLogTail(file, bytes); } finally { inFlight -= 1; }
      });

      const panel = openPanel(ps);
      await panel.mounted;
      await vi.waitFor(() => expect(panel.text()).toContain("OUT-"));
      for (const row of rows) expect(panel.text()).toContain(row.bash_id);
      expect(previewReads.mock.calls.map(([file]) => file)).toEqual([rows[0]!.log_path]);

      let release!: () => void;
      gate = new Promise<void>((resolve) => { release = resolve; });
      panel.input("j");
      await vi.waitFor(() => expect(previewReads).toHaveBeenCalledTimes(2));
      panel.input("j"); panel.input("k"); panel.input("j");
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(previewReads).toHaveBeenCalledTimes(2);

      release();
      await vi.waitFor(() => expect(panel.text()).toContain(`> ${rows[2]!.bash_id}`));
      await vi.waitFor(() => expect(panel.text()).toContain(outputOf(rows[2]!)));
      expect(panel.text()).not.toContain(outputOf(rows[1]!));
      expect(previewReads.mock.calls.map(([file]) => file)).toEqual([rows[0]!.log_path, rows[1]!.log_path, rows[2]!.log_path]);
      expect(maxInFlight).toBe(1);
      panel.input("q");
      await panel.closed;
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });

  it("shows an unreadable preview as that row's error without failing the task list", async () => {
    const { root, runtime, ps, rows } = await setup();
    try {
      await rm(rows[0]!.log_path);
      await mkdir(rows[0]!.log_path); // open succeeds; reading a directory fails with EISDIR
      const panel = openPanel(ps);
      await panel.mounted;
      await vi.waitFor(() => expect(panel.text()).toContain("Output unavailable"));
      for (const row of rows) expect(panel.text()).toContain(row.bash_id);
      panel.input("j");
      await vi.waitFor(() => expect(panel.text()).toContain(outputOf(rows[1]!)));
      expect(panel.text()).not.toContain("Output unavailable");
      panel.input("q");
      await panel.closed;
    } finally { await runtime.shutdown(); await rm(root, { recursive: true, force: true }); }
  });
});
