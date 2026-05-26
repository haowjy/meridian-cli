import type { Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";

import {
  borderLine,
  padLine,
  renderFooter,
  titleLine,
  type PanelCommandContext,
} from "./selectable_panel";

export type LogOverlayStream = {
  id: string;
  label: string;
  loadText: () => Promise<string>;
};

export type LogOverlayOptions = {
  title: string;
  streams: LogOverlayStream[];
  footer?: string;
  initialFollow?: boolean;
  refreshIntervalMs?: number;
};

type PanelTui = {
  requestRender: () => void;
  terminal?: { rows?: number };
};

const LOG_OVERLAY_OPTIONS = {
  overlay: true,
  overlayOptions: {
    width: "90%",
    maxHeight: "80%",
    anchor: "center",
  },
};

function decodeKittyPrintable(data: string): string | undefined {
  const csiU = data.match(/^\x1b\[(\d{1,8})u$/);
  if (!csiU) return undefined;
  const codepoint = Number.parseInt(csiU[1] ?? "", 10);
  if (!Number.isFinite(codepoint) || codepoint < 32 || codepoint >= 0x110000) return undefined;
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

function isUp(data: string): boolean {
  const ch = printableChar(data);
  return data === "\x1b[A" || ch === "k" || ch === "K";
}

function isDown(data: string): boolean {
  const ch = printableChar(data);
  return data === "\x1b[B" || ch === "j" || ch === "J";
}

function isTop(data: string): boolean {
  return printableChar(data) === "g";
}

function isBottom(data: string): boolean {
  return printableChar(data) === "G";
}

function isFollowToggle(data: string): boolean {
  const ch = printableChar(data);
  return ch === "f" || ch === "F";
}

function isStreamToggle(data: string): boolean {
  const ch = printableChar(data);
  return ch === "s" || ch === "S";
}

class LogOverlayComponent implements Component {
  private text = "Loading…";
  private closed = false;
  private follow: boolean;
  private anchorEnd: number | null = null;
  private refreshTimer: NodeJS.Timeout | null = null;
  private refreshInFlight = false;
  private streamIndex = 0;

  constructor(
    private readonly tui: PanelTui,
    private readonly theme: Theme,
    private readonly options: LogOverlayOptions,
    private readonly done: () => void,
  ) {
    this.follow = options.initialFollow === true;
    void this.refresh();
    this.refreshTimer = setInterval(() => {
      if (this.follow) void this.refresh();
    }, options.refreshIntervalMs ?? 1000);
  }

  handleInput(data: string): void {
    if (this.closed) return;
    if (isQuit(data)) {
      this.close();
      return;
    }
    if (isRefresh(data)) {
      void this.refresh();
      return;
    }
    if (isTop(data)) {
      this.follow = false;
      this.anchorEnd = Math.min(this.visibleTextLines(), this.textLines().length);
      this.tui.requestRender();
      return;
    }
    if (isBottom(data)) {
      this.follow = false;
      this.anchorEnd = this.textLines().length;
      this.tui.requestRender();
      return;
    }
    if (isFollowToggle(data)) {
      this.follow = !this.follow;
      this.anchorEnd = this.follow ? null : this.textLines().length;
      this.tui.requestRender();
      return;
    }
    if (this.hasMultipleStreams() && isStreamToggle(data)) {
      this.cycleStream();
      return;
    }
    if (isUp(data)) {
      this.scrollBy(-1);
      return;
    }
    if (isDown(data)) this.scrollBy(1);
  }

  invalidate(): void {
    // No cached render state to clear.
  }

  dispose(): void {
    this.clearTimer();
  }

  render(width: number): string[] {
    const textLines = this.textLines();
    const maxTextLines = this.visibleTextLines();
    const end = this.resolveEnd(textLines.length, maxTextLines);
    const start = Math.max(0, end - maxTextLines);
    const visible = textLines.slice(start, end);
    while (visible.length < maxTextLines) visible.push("");

    const lines = [titleLine(this.options.title, width, this.theme)];
    for (const line of visible) lines.push(padLine(line, width, this.theme));
    lines.push(borderLine(width, "├", "─", "┤", this.theme));
    lines.push(padLine(this.renderStatus(start, end, textLines.length), width, this.theme));
    lines.push(padLine(renderFooter(this.options.footer ?? this.defaultFooter(), this.theme), width, this.theme));
    lines.push(borderLine(width, "╰", "─", "╯", this.theme));
    return lines;
  }

  private close(): void {
    if (this.closed) return;
    this.closed = true;
    this.clearTimer();
    this.done();
  }

  private clearTimer(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    this.refreshTimer = null;
  }

  private scrollBy(delta: number): void {
    const total = this.textLines().length;
    const visible = this.visibleTextLines();
    const currentEnd = this.anchorEnd ?? total;
    const minEnd = Math.min(visible, total);
    this.follow = false;
    this.anchorEnd = Math.max(minEnd, Math.min(total, currentEnd + delta));
    this.tui.requestRender();
  }

  private cycleStream(): void {
    this.streamIndex = (this.streamIndex + 1) % this.streams().length;
    this.anchorEnd = null;
    void this.refresh();
  }

  private streams(): LogOverlayStream[] {
    return this.options.streams.length > 0
      ? this.options.streams
      : [{ id: "default", label: "log", loadText: async () => "" }];
  }

  private currentStream(): LogOverlayStream {
    return this.streams()[this.streamIndex] ?? this.streams()[0]!;
  }

  private hasMultipleStreams(): boolean {
    return this.streams().length > 1;
  }

  private textLines(): string[] {
    const lines = this.text.split(/\r?\n/);
    if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
    return lines.length > 0 ? lines : [""];
  }

  private visibleTextLines(): number {
    const terminalRows = this.tui.terminal?.rows ?? 24;
    return Math.max(1, Math.floor(terminalRows * 0.8) - 5);
  }

  private resolveEnd(total: number, visible: number): number {
    if (this.follow) return total;
    const minEnd = Math.min(visible, total);
    return Math.max(minEnd, Math.min(total, this.anchorEnd ?? total));
  }

  private renderStatus(start: number, end: number, total: number): string {
    const stream = this.hasMultipleStreams() ? `${this.currentStream().label}  ` : "";
    if (this.follow) return `${this.theme.fg("dim", stream)}${this.theme.fg("accent", "following")}`;
    if (total <= 0) return `${this.theme.fg("dim", stream)}${this.theme.fg("dim", "empty")}`;
    const percent = Math.round((end / total) * 100);
    return this.theme.fg("dim", `${stream}${percent}%  L${Math.min(start + 1, total)}-${end}/${total}`);
  }

  private defaultFooter(): string {
    const stream = this.hasMultipleStreams() ? "s stream · " : "";
    return `j/k scroll · g/G top/bot · ${stream}f follow · r refresh · q close`;
  }

  private async refresh(): Promise<void> {
    if (this.refreshInFlight) return;
    this.refreshInFlight = true;
    try {
      this.text = await this.currentStream().loadText();
      if (!this.follow) {
        const total = this.textLines().length;
        const visible = this.visibleTextLines();
        this.anchorEnd = Math.max(Math.min(visible, total), Math.min(total, this.anchorEnd ?? total));
      }
    } catch (error) {
      this.text = `Failed to load text: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      this.refreshInFlight = false;
    }
    if (!this.closed) this.tui.requestRender();
  }
}

export async function openLogOverlay(
  ctx: PanelCommandContext,
  options: LogOverlayOptions,
): Promise<void> {
  if (!ctx.ui?.custom || ctx.hasUI === false) return;
  await ctx.ui.custom<void>(
    (tui, theme, _keybindings, done) =>
      new LogOverlayComponent(tui, theme, options, () => done(undefined)),
    LOG_OVERLAY_OPTIONS,
  );
}
