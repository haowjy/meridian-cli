import type { Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth, type Component } from "@earendil-works/pi-tui";

export type SelectablePanelColumn<Row> = {
  header: string;
  width: number;
  align?: "left" | "right";
  render: (row: Row, theme?: Theme, selected?: boolean) => string;
};

export type SelectablePanelOptions<Row> = {
  title: string;
  columns: SelectablePanelColumn<Row>[];
  loadRows: () => Promise<Row[]>;
  getRowId: (row: Row) => string;
  renderPreview: (row: Row, theme: Theme) => string[];
  onEnter?: (row: Row) => void | Promise<void>;
  emptyMessage?: string;
  footer?: string;
  maxRows?: number;
};

export type TextOverlayOptions = {
  title: string;
  loadText: () => Promise<string>;
  footer?: string;
};

type PanelTui = {
  requestRender: () => void;
  terminal?: { rows?: number };
};

type PanelUi = {
  custom: <T>(
    factory: (
      tui: PanelTui,
      theme: Theme,
      keybindings: unknown,
      done: (value: T) => void,
    ) => Component,
    options?: Record<string, unknown>,
  ) => Promise<T>;
};

export type PanelCommandContext = {
  hasUI?: boolean;
  ui?: PanelUi;
};

export const TEXT_OVERLAY_OPTIONS = {
  overlay: true,
  overlayOptions: {
    width: "100%",
    maxHeight: "100%",
  },
};

function decodeKittyPrintable(data: string): string | undefined {
  const csiU = data.match(/^\x1b\[(\d{1,8})u$/);
  if (!csiU) return undefined;
  const codepoint = Number.parseInt(csiU[1] ?? "", 10);
  if (!Number.isFinite(codepoint) || codepoint < 32 || codepoint >= 0x110000) {
    return undefined;
  }
  return String.fromCodePoint(codepoint);
}

function printableChar(data: string): string | undefined {
  const kitty = decodeKittyPrintable(data);
  if (kitty) return kitty;
  if (data.length === 1 && data.charCodeAt(0) >= 32) return data;
  return undefined;
}

function isQuit(data: string): boolean {
  const ch = printableChar(data);
  return data === "\x1b" || ch === "q" || ch === "Q";
}

function isRefresh(data: string): boolean {
  const ch = printableChar(data);
  return ch === "r" || ch === "R";
}

function isConfirm(data: string): boolean {
  return data === "\n" || data === "\r";
}

function isUp(data: string): boolean {
  const ch = printableChar(data);
  return data === "\x1b[A" || ch === "k" || ch === "K";
}

function isDown(data: string): boolean {
  const ch = printableChar(data);
  return data === "\x1b[B" || ch === "j" || ch === "J";
}

function fitCell(value: string, width: number, align: "left" | "right" = "left"): string {
  const truncated = truncateToWidth(value, Math.max(0, width), "", true);
  const pad = Math.max(0, width - visibleWidth(truncated));
  return align === "right" ? " ".repeat(pad) + truncated : truncated + " ".repeat(pad);
}

function borderLine(width: number, left: string, fill: string, right: string, theme: Theme): string {
  if (width <= 0) return "";
  if (width === 1) return theme.fg("dim", fill);
  return theme.fg("dim", `${left}${fill.repeat(Math.max(0, width - 2))}${right}`);
}

function titleLine(title: string, width: number, theme: Theme): string {
  if (width <= 0) return "";
  if (width === 1) return theme.fg("dim", "─");
  const innerWidth = Math.max(0, width - 2);
  const label = ` ${title} `;
  const truncated = truncateToWidth(label, innerWidth, "", true);
  const remaining = Math.max(0, innerWidth - visibleWidth(truncated));
  return theme.fg("dim", `┌${truncated}${"─".repeat(remaining)}┐`);
}

function padLine(content: string, width: number, theme: Theme): string {
  const innerWidth = Math.max(0, width - 2);
  const truncated = truncateToWidth(content, innerWidth, "", true);
  const pad = Math.max(0, innerWidth - visibleWidth(truncated));
  return `${theme.fg("dim", "│")}${truncated}${" ".repeat(pad)}${theme.fg("dim", "│")}`;
}

function renderTableLine<Row>(
  row: Row | null,
  columns: SelectablePanelColumn<Row>[],
  width: number,
  theme: Theme,
  selected: boolean,
  suffix = "",
): string {
  const innerWidth = Math.max(0, width - 2);
  const prefix = row == null ? "  " : selected ? theme.fg("accent", "> ") : "  ";
  const cells = columns.map((column) => {
    const value = row == null ? column.header : column.render(row, theme, selected);
    const rendered = row == null ? theme.fg("dim", value) : value;
    return fitCell(rendered, column.width, column.align);
  });
  return padLine(truncateToWidth(prefix + cells.join("  ") + suffix, innerWidth, "", true), width, theme);
}

export function renderSelectablePanel<Row>(
  width: number,
  terminalRows: number,
  theme: Theme,
  options: SelectablePanelOptions<Row>,
  rows: Row[],
  selectedIndex: number,
  rowScrollOffset: number,
): string[] {
  const footer = options.footer ?? "enter open · j/k select · r refresh · q close";
  const maxRows = Math.max(1, options.maxRows ?? 8);
  const selected = rows[selectedIndex] ?? null;
  const previewRaw = selected ? options.renderPreview(selected, theme) : [theme.fg("dim", options.emptyMessage ?? "No rows")];
  const previewLimit = Math.max(1, Math.min(5, terminalRows - maxRows - 7));
  const preview = previewRaw.slice(0, previewLimit);
  while (preview.length < previewLimit) preview.push("");

  const visibleCount = Math.min(maxRows, rows.length);
  const start = Math.max(0, Math.min(rowScrollOffset, Math.max(0, rows.length - visibleCount)));
  const end = start + visibleCount;
  const visibleRows = rows.slice(start, end);
  const headerSuffix = rows.length > visibleCount && visibleCount > 0 ? theme.fg("dim", ` [${start + 1}-${end}/${rows.length}]`) : "";

  const topLines: string[] = [titleLine(options.title, width, theme), padLine("", width, theme)];
  for (const line of preview) topLines.push(padLine(line, width, theme));

  const tableLines: string[] = [borderLine(width, "├", "─", "┤", theme)];
  tableLines.push(renderTableLine(null, options.columns, width, theme, false, headerSuffix));
  tableLines.push(borderLine(width, "├", "─", "┤", theme));
  if (rows.length === 0) {
    tableLines.push(padLine(theme.fg("dim", options.emptyMessage ?? "No rows"), width, theme));
  } else {
    for (let idx = 0; idx < visibleRows.length; idx += 1) {
      tableLines.push(renderTableLine(visibleRows[idx]!, options.columns, width, theme, start + idx === selectedIndex));
    }
  }

  const footerLines = [borderLine(width, "├", "─", "┤", theme), padLine(theme.fg("dim", footer), width, theme)];
  const targetRows = Math.max(16, terminalRows - 1);
  const spacerCount = Math.max(0, targetRows - topLines.length - tableLines.length - footerLines.length);
  const spacerLines = Array.from({ length: spacerCount }, () => padLine("", width, theme));

  return [...topLines, ...spacerLines, ...tableLines, ...footerLines];
}

export class SelectablePanelComponent<Row> implements Component {
  private rows: Row[] = [];
  private selectedIndex = 0;
  private rowScrollOffset = 0;
  private closed = false;
  private loading = false;
  private errorMessage: string | null = null;

  constructor(
    private readonly tui: PanelTui,
    private readonly theme: Theme,
    private readonly options: SelectablePanelOptions<Row>,
    private readonly done: () => void,
  ) {
    void this.refresh();
  }

  handleInput(data: string): void {
    if (this.closed) return;
    if (isQuit(data)) {
      this.closed = true;
      this.done();
      return;
    }
    if (isRefresh(data)) {
      void this.refresh();
      return;
    }
    if (isUp(data)) {
      this.selectedIndex = Math.max(0, this.selectedIndex - 1);
      this.ensureSelectedVisible();
      this.tui.requestRender();
      return;
    }
    if (isDown(data)) {
      this.selectedIndex = Math.min(Math.max(0, this.rows.length - 1), this.selectedIndex + 1);
      this.ensureSelectedVisible();
      this.tui.requestRender();
      return;
    }
    if (isConfirm(data)) {
      const row = this.rows[this.selectedIndex];
      if (row && this.options.onEnter) {
        void Promise.resolve(this.options.onEnter(row))
          .then(() => this.refresh())
          .catch((error: unknown) => {
            this.errorMessage = `Action failed: ${error instanceof Error ? error.message : String(error)}`;
            this.tui.requestRender();
          });
      }
    }
  }

  invalidate(): void {
    this.tui.requestRender();
  }

  render(width: number): string[] {
    if (this.loading && this.rows.length === 0) {
      return [
        titleLine(this.options.title, width, this.theme),
        padLine(this.theme.fg("dim", "Loading…"), width, this.theme),
        borderLine(width, "╰", "─", "╯", this.theme),
      ];
    }
    return renderSelectablePanel(
      width,
      this.tui.terminal?.rows ?? 24,
      this.theme,
      this.errorMessage ? { ...this.options, emptyMessage: this.errorMessage } : this.options,
      this.rows,
      this.selectedIndex,
      this.rowScrollOffset,
    );
  }

  private async refresh(): Promise<void> {
    if (this.closed || this.loading) return;
    this.loading = true;
    const selectedId = this.rows[this.selectedIndex] ? this.options.getRowId(this.rows[this.selectedIndex]!) : null;
    try {
      const nextRows = await this.options.loadRows();
      this.errorMessage = null;
      this.rows = nextRows;
      if (selectedId) {
        const nextIndex = nextRows.findIndex((row) => this.options.getRowId(row) === selectedId);
        this.selectedIndex = nextIndex >= 0 ? nextIndex : Math.min(this.selectedIndex, Math.max(0, nextRows.length - 1));
      } else {
        this.selectedIndex = Math.min(this.selectedIndex, Math.max(0, nextRows.length - 1));
      }
      this.ensureSelectedVisible();
    } catch (error) {
      this.rows = [];
      this.errorMessage = `Failed to load rows: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      this.loading = false;
      if (!this.closed) this.tui.requestRender();
    }
  }

  private ensureSelectedVisible(): void {
    const maxRows = Math.max(1, this.options.maxRows ?? 8);
    if (this.selectedIndex < this.rowScrollOffset) {
      this.rowScrollOffset = this.selectedIndex;
    } else if (this.selectedIndex >= this.rowScrollOffset + maxRows) {
      this.rowScrollOffset = this.selectedIndex - maxRows + 1;
    }
    this.rowScrollOffset = Math.max(0, Math.min(this.rowScrollOffset, Math.max(0, this.rows.length - maxRows)));
  }
}

export async function openSelectablePanel<Row>(
  ctx: PanelCommandContext,
  options: SelectablePanelOptions<Row>,
): Promise<void> {
  if (!ctx.ui?.custom || ctx.hasUI === false) {
    return;
  }
  await ctx.ui.custom<void>((tui, theme, _keybindings, done) =>
    new SelectablePanelComponent(tui, theme, options, () => done(undefined)),
  );
}

class TextOverlayComponent implements Component {
  private text = "Loading…";
  private closed = false;

  constructor(
    private readonly tui: PanelTui,
    private readonly theme: Theme,
    private readonly options: TextOverlayOptions,
    private readonly done: () => void,
  ) {
    void this.refresh();
  }

  handleInput(data: string): void {
    if (this.closed) return;
    if (isQuit(data)) {
      this.closed = true;
      this.done();
      return;
    }
    if (isRefresh(data)) void this.refresh();
  }

  invalidate(): void {
    this.tui.requestRender();
  }

  render(width: number): string[] {
    const terminalRows = this.tui.terminal?.rows ?? 24;
    const maxTextLines = Math.max(1, terminalRows - 4);
    const rawLines = this.text.split(/\r?\n/);
    const lines = [titleLine(this.options.title, width, this.theme)];
    for (const line of rawLines.slice(-maxTextLines)) {
      lines.push(padLine(line, width, this.theme));
    }
    lines.push(borderLine(width, "├", "─", "┤", this.theme));
    lines.push(padLine(this.theme.fg("dim", this.options.footer ?? "r refresh · q close"), width, this.theme));
    lines.push(borderLine(width, "╰", "─", "╯", this.theme));
    return lines;
  }

  private async refresh(): Promise<void> {
    try {
      this.text = await this.options.loadText();
    } catch (error) {
      this.text = `Failed to load text: ${error instanceof Error ? error.message : String(error)}`;
    }
    if (!this.closed) this.tui.requestRender();
  }
}

export async function openTextOverlay(
  ctx: PanelCommandContext,
  options: TextOverlayOptions,
): Promise<void> {
  if (!ctx.ui?.custom || ctx.hasUI === false) {
    return;
  }
  await ctx.ui.custom<void>(
    (tui, theme, _keybindings, done) =>
      new TextOverlayComponent(tui, theme, options, () => done(undefined)),
    TEXT_OVERLAY_OPTIONS,
  );
}
