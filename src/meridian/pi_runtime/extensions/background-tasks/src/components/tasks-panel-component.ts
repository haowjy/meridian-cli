import type { Theme } from "@earendil-works/pi-coding-agent";
import { type Component, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

import { backgroundForegroundBash } from "../background_foreground";
import type { TaskPanelHost } from "../panel/host";
import {
  isPanelBackground,
  isPanelClear,
  isPanelConfirm,
  isPanelDown,
  isPanelKill,
  isPanelLogScrollDown,
  isPanelLogScrollUp,
  isPanelQuit,
  isPanelUp,
} from "../panel/input";
import { formatPingBadge, formatPingDetailLines } from "../panel/ping_format";
import { panelConfig } from "../panel/config";
import type { PanelEntry } from "../panel/types";
import { stripAnsi } from "../utils/ansi";
import {
  createPanelPadder,
  renderPanelRule,
  renderPanelTitleLine,
} from "./panel-helpers";
import { kindBadge, statusIcon, statusLabel } from "./status-format";

function formatRuntime(startTime: number, endTime: number | null): string {
  if (startTime <= 0) {
    return "-";
  }
  const end = endTime ?? Date.now();
  const ms = end - startTime;
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)}KB`;
  }
  return `${bytes}B`;
}

function truncate(str: string, maxLen: number): string {
  if (maxLen <= 3) {
    return str.slice(0, maxLen);
  }
  if (str.length <= maxLen) {
    return str;
  }
  return `${str.slice(0, maxLen - 3)}...`;
}

function fitCell(value: string, width: number, align: "left" | "right" = "left"): string {
  const truncated = truncateToWidth(value, Math.max(0, width));
  const pad = Math.max(0, width - visibleWidth(truncated));
  if (align === "right") {
    return " ".repeat(pad) + truncated;
  }
  return truncated + " ".repeat(pad);
}

export class TasksPanelComponent implements Component {
  private readonly tui: { requestRender: () => void };
  private readonly theme: Theme;
  private readonly onClose: (taskId?: string) => void;
  private readonly host: TaskPanelHost;

  private entries: PanelEntry[] = [];
  private selectedIndex = 0;
  private processScrollOffset = 0;
  private logScrollOffset = 0;
  private scrollInfo = { above: 0, below: 0 };
  private cachedLines: string[] = [];
  private cachedWidth = 0;
  private unsubscribe: (() => void) | null = null;

  constructor(
    tui: { requestRender: () => void },
    theme: Theme,
    onClose: (taskId?: string) => void,
    host: TaskPanelHost,
  ) {
    this.tui = tui;
    this.theme = theme;
    this.onClose = onClose;
    this.host = host;

    void this.refreshEntries();
    this.unsubscribe = this.host.onEvent(() => {
      void this.refreshEntries();
    });
  }

  private async refreshEntries(): Promise<void> {
    this.entries = await this.host.list();
    this.host.setSyncEntries(this.entries);
    if (this.selectedIndex >= this.entries.length) {
      this.selectedIndex = Math.max(0, this.entries.length - 1);
    }
    this.ensureProcessVisible(this.entries.length);
    this.invalidate();
    this.tui.requestRender();
  }

  handleInput(data: string): void {
    const processes = this.entries;

    if (isPanelQuit(data)) {
      this.unsubscribe?.();
      this.unsubscribe = null;
      this.onClose();
      return;
    }

    if (isPanelDown(data)) {
      if (processes.length > 0) {
        this.selectedIndex = Math.min(this.selectedIndex + 1, processes.length - 1);
        this.logScrollOffset = 0;
        this.ensureProcessVisible(processes.length);
        this.invalidate();
        this.tui.requestRender();
      }
      return;
    }

    if (isPanelUp(data)) {
      if (processes.length > 0) {
        this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
        this.logScrollOffset = 0;
        this.ensureProcessVisible(processes.length);
        this.invalidate();
        this.tui.requestRender();
      }
      return;
    }

    if (isPanelLogScrollUp(data)) {
      this.logScrollOffset = Math.max(0, this.logScrollOffset - 5);
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    if (isPanelLogScrollDown(data)) {
      this.logScrollOffset += 5;
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    if (isPanelConfirm(data)) {
      const proc = processes[this.selectedIndex];
      if (proc) {
        this.unsubscribe?.();
        this.unsubscribe = null;
        this.onClose(proc.id);
      }
      return;
    }

    if (isPanelKill(data)) {
      const proc = processes[this.selectedIndex];
      if (proc?.isLive) {
        void this.host.kill(proc.id).then(() => this.refreshEntries());
      }
      return;
    }

    if (isPanelClear(data)) {
      void this.host.clearFinished().then(() => this.refreshEntries());
      return;
    }

    if (isPanelBackground(data)) {
      const proc = processes[this.selectedIndex];
      if (proc?.isForeground) {
        void backgroundForegroundBash(this.host).then(() => this.refreshEntries());
      }
    }
  }

  private ensureProcessVisible(totalProcesses: number): void {
    const maxVisibleProcesses = panelConfig.processList.maxVisibleProcesses;
    const visibleCount = Math.min(maxVisibleProcesses, totalProcesses);
    if (this.selectedIndex < this.processScrollOffset) {
      this.processScrollOffset = this.selectedIndex;
    } else if (this.selectedIndex >= this.processScrollOffset + visibleCount) {
      this.processScrollOffset = this.selectedIndex - visibleCount + 1;
    }
    this.processScrollOffset = Math.max(
      0,
      Math.min(this.processScrollOffset, Math.max(0, totalProcesses - visibleCount)),
    );
  }

  invalidate(): void {
    this.cachedWidth = 0;
    this.cachedLines = [];
  }

  render(width: number): string[] {
    if (width === this.cachedWidth && this.cachedLines.length > 0) {
      return this.cachedLines;
    }

    const cfg = panelConfig.processList;
    const maxVisibleProcesses = cfg.maxVisibleProcesses;
    const maxPreviewLines = cfg.maxPreviewLines;

    const theme = this.theme;
    const dim = (s: string) => theme.fg("dim", s);
    const accent = (s: string) => theme.fg("accent", s);
    const warning = (s: string) => theme.fg("warning", s);

    const lines: string[] = [];
    const processes = this.entries;
    const innerWidth = width - 2;

    const basePadLine = createPanelPadder(width);
    const padLine = (content: string): string =>
      basePadLine(
        visibleWidth(content) > innerWidth
          ? truncateToWidth(content, innerWidth, "", true)
          : content,
      );

    lines.push(renderPanelTitleLine("Tasks & Spawns", width, theme));

    if (processes.length === 0) {
      lines.push(padLine(""));
      lines.push(padLine(dim("No tasks or spawns")));
      lines.push(padLine(dim("Start work with $, background_task, or meridian spawn")));
      lines.push(padLine(""));
    } else {
      const prefixWidth = 2;
      const minTotalWidth = 44;
      const scaleFactor = innerWidth < minTotalWidth ? innerWidth / minTotalWidth : 1;
      const processWidth = Math.max(14, Math.floor(22 * scaleFactor));
      const statusWidth = Math.max(12, Math.floor(20 * scaleFactor));
      const timeWidth = Math.max(4, Math.floor(8 * scaleFactor));
      const sizeWidth = Math.max(4, Math.floor(8 * scaleFactor));

      const hasProcessScroll = processes.length > maxVisibleProcesses;
      const headerSuffixText = hasProcessScroll
        ? ` [${this.processScrollOffset + 1}-${Math.min(this.processScrollOffset + maxVisibleProcesses, processes.length)}/${processes.length}]`
        : "";
      const headerSuffixLen = hasProcessScroll ? headerSuffixText.length : 0;

      const fixedWidth =
        prefixWidth + processWidth + statusWidth + timeWidth + sizeWidth + headerSuffixLen;
      const cmdWidth = Math.max(4, innerWidth - fixedWidth);

      lines.push(padLine(""));
      const header =
        "  " +
        dim("Process".padEnd(processWidth)) +
        dim("Command".padEnd(cmdWidth)) +
        dim("Status".padEnd(statusWidth)) +
        dim("Time".padEnd(timeWidth)) +
        dim("Size".padStart(sizeWidth)) +
        (hasProcessScroll ? dim(headerSuffixText) : "");
      lines.push(padLine(header));
      lines.push(renderPanelRule(width, theme));

      const visibleProcessCount = Math.min(maxVisibleProcesses, processes.length);
      const startIdx = this.processScrollOffset;
      const endIdx = startIdx + visibleProcessCount;

      for (let i = startIdx; i < endIdx; i++) {
        const proc = processes[i];
        if (!proc) {
          continue;
        }
        const isSelected = i === this.selectedIndex;
        const sizes = this.host.getFileSize(proc.id);
        const totalSize = sizes ? sizes.stdout + sizes.stderr : proc.logBytes;

        const statusText = this.formatStatus(proc);
        const badge = kindBadge(proc);
        const idPlain = `(${proc.id})`;
        const maxNameLen = Math.max(1, processWidth - visibleWidth(idPlain) - visibleWidth(badge) - 2);
        const tName = truncate(proc.name, maxNameLen);
        const processCell = isSelected
          ? `${accent(`[${badge}]`)} ${accent(tName)} ${dim(idPlain)}`
          : `${dim(`[${badge}]`)} ${tName}${dim(` ${idPlain}`)}`;

        const row =
          fitCell(processCell, processWidth) +
          fitCell(truncate(proc.command, cmdWidth - 1), cmdWidth) +
          fitCell(statusText, statusWidth) +
          fitCell(formatRuntime(proc.startTime, proc.endTime), timeWidth) +
          fitCell(formatBytes(totalSize), sizeWidth, "right");

        lines.push(padLine(isSelected ? `${accent(">")} ${row}` : `  ${row}`));
      }

      for (let i = visibleProcessCount; i < maxVisibleProcesses; i++) {
        lines.push(padLine(""));
      }

      const selected = processes[this.selectedIndex];
      if (selected) {
        const output = this.host.getOutput(selected.id, maxPreviewLines * 2);
        lines.push(renderPanelRule(width, theme));

        const logTitle = `Output: ${accent(selected.name)} ${dim(`(${selected.id})`)}`;
        lines.push(padLine(truncateToWidth(logTitle, innerWidth, "", true)));

        for (const pingLine of formatPingDetailLines(selected)) {
          lines.push(padLine(dim(pingLine)));
        }
        if (selected.isForeground) {
          lines.push(padLine(warning("Foreground — blocks $ until backgrounded or exit")));
        }
        lines.push(padLine(""));

        let renderedLines = 0;
        if (output) {
          const logLines: { type: "stdout" | "stderr"; text: string }[] = [];
          for (const line of output.stdout) {
            logLines.push({ type: "stdout", text: line });
          }
          for (const line of output.stderr) {
            logLines.push({ type: "stderr", text: line });
          }

          if (logLines.length === 0) {
            lines.push(padLine(dim("(no output yet)")));
            renderedLines = 1;
          } else {
            const logStart = Math.max(
              0,
              logLines.length - maxPreviewLines - this.logScrollOffset,
            );
            const logEnd = Math.max(0, logLines.length - this.logScrollOffset);
            const visibleLines = logLines.slice(logStart, logEnd);
            this.scrollInfo.above = logStart;
            this.scrollInfo.below =
              this.logScrollOffset > 0 ? logLines.length - logEnd : 0;

            for (const line of visibleLines) {
              const displayLine = truncate(stripAnsi(line.text), innerWidth - 2);
              lines.push(padLine(line.type === "stderr" ? warning(displayLine) : displayLine));
              renderedLines++;
            }
          }
        }

        while (renderedLines < maxPreviewLines) {
          lines.push(padLine(""));
          renderedLines++;
        }
      }
    }

    const hasForeground = processes.some((proc) => proc.isForeground);
    const footerLeft =
      `${dim("enter")} stream  ` +
      `${dim("j/k")} select  ` +
      (hasForeground
        ? `${warning("b")} background  ${dim("ctrl+b×2")}  `
        : "") +
      `${dim("x")} kill/cancel  ` +
      `${dim("c")} clear  ` +
      `${dim("q")} quit`;

    let footerRight = "";
    if (this.scrollInfo.above > 0 || this.scrollInfo.below > 0) {
      const parts: string[] = [];
      if (this.scrollInfo.above > 0) {
        parts.push(`↑${this.scrollInfo.above}`);
      }
      if (this.scrollInfo.below > 0) {
        parts.push(`↓${this.scrollInfo.below}`);
      }
      footerRight = `${dim("J/K")} scroll ${dim(parts.join(" "))}`;
    }

    const footerLeftLen = visibleWidth(footerLeft);
    const footerRightLen = visibleWidth(footerRight);
    const footerGap = Math.max(2, innerWidth - footerLeftLen - footerRightLen);
    let footer = footerLeft + " ".repeat(footerGap) + footerRight;
    if (footerLeftLen + footerGap + footerRightLen > innerWidth) {
      footer = truncateToWidth(footer, innerWidth);
    }

    lines.push(renderPanelRule(width, theme));
    lines.push(padLine(footer));

    this.cachedLines = lines;
    this.cachedWidth = width;
    return this.cachedLines;
  }

  private formatStatus(proc: PanelEntry): string {
    const theme = this.theme;
    const dim = (s: string) => theme.fg("dim", s);
    const success = (s: string) => theme.fg("success", s);
    const warn = (s: string) => theme.fg("warning", s);
    const error = (s: string) => theme.fg("error", s);

    const icon = statusIcon(proc.status, proc.success);
    const label = statusLabel(proc);
    const ping = formatPingBadge(proc);
    const pingSuffix = ping ? dim(` ${ping}`) : "";
    const fgPrefix = proc.isForeground ? warn("● fg ") : "";

    if (proc.isLive) {
      return fgPrefix + success(`${icon} ${label}`) + pingSuffix;
    }
    if (proc.success === false) {
      return fgPrefix + error(`${icon} ${label}`) + pingSuffix;
    }
    return fgPrefix + dim(`${icon} ${label}`) + pingSuffix;
  }
}
