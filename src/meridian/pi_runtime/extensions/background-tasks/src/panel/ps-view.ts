import type { Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

import {
  createPanelPadder,
  renderPanelRule,
  renderPanelTitleLine,
} from "../components/panel-helpers";
import { kindBadge, statusIcon, statusLabel } from "../components/status-format";
import { stripAnsi } from "../utils/ansi";
import { formatTaskDetailLines } from "./detail_format";
import type { TaskPanelHost } from "./host";
import { computePanelLayout } from "./layout";
import type { PanelEntry } from "./types";

export type PsPanelActions = {
  onQuit: () => void;
  /** Open log stream overlay; /ps stays open underneath. */
  onOpenStream: (taskId: string) => void | Promise<void>;
};

export type PsViewModel = {
  entries: PanelEntry[];
  selectedIndex: number;
  processScrollOffset: number;
  logScrollOffset: number;
  backgroundingForeground: boolean;
  /** Updated while rendering the log preview (footer scroll hints). */
  logScroll: { above: number; below: number };
};

export type PsColumnLayout = {
  processWidth: number;
  cmdWidth: number;
  statusWidth: number;
  timeWidth: number;
  sizeWidth: number;
  headerSuffixText: string;
};

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

/** Column widths for process table — exported for unit tests. */
export function computePsColumnLayout(
  innerWidth: number,
  processCount: number,
  maxVisibleProcesses: number,
  processScrollOffset: number,
): PsColumnLayout {
  const prefixWidth = 2;
  const minTotalWidth = 44;
  const scaleFactor = innerWidth < minTotalWidth ? innerWidth / minTotalWidth : 1;
  const processWidth = Math.max(14, Math.floor(22 * scaleFactor));
  const statusWidth = Math.max(12, Math.floor(20 * scaleFactor));
  const timeWidth = Math.max(4, Math.floor(8 * scaleFactor));
  const sizeWidth = Math.max(4, Math.floor(8 * scaleFactor));

  const hasProcessScroll = processCount > maxVisibleProcesses;
  const headerSuffixText = hasProcessScroll
    ? ` [${processScrollOffset + 1}-${Math.min(processScrollOffset + maxVisibleProcesses, processCount)}/${processCount}]`
    : "";
  const headerSuffixLen = hasProcessScroll ? headerSuffixText.length : 0;
  const fixedWidth =
    prefixWidth + processWidth + statusWidth + timeWidth + sizeWidth + headerSuffixLen;
  const cmdWidth = Math.max(4, innerWidth - fixedWidth);

  return {
    processWidth,
    cmdWidth,
    statusWidth,
    timeWidth,
    sizeWidth,
    headerSuffixText,
  };
}

function formatStatus(theme: Theme, proc: PanelEntry): string {
  const dim = (s: string) => theme.fg("dim", s);
  const success = (s: string) => theme.fg("success", s);
  const warn = (s: string) => theme.fg("warning", s);
  const error = (s: string) => theme.fg("error", s);

  const icon = statusIcon(proc.status, proc.success);
  const label = statusLabel(proc);
  const fgPrefix = proc.isForeground ? warn("● fg ") : "";

  if (proc.isLive) {
    return fgPrefix + success(`${icon} ${label}`);
  }
  if (proc.success === false) {
    return fgPrefix + error(`${icon} ${label}`);
  }
  return fgPrefix + dim(`${icon} ${label}`);
}

/** Pure render: preview on top, process list + footer pinned to bottom. */
export function renderPsPanel(
  width: number,
  terminalRows: number,
  theme: Theme,
  host: TaskPanelHost,
  model: PsViewModel,
): string[] {
  const processes = model.entries;
  const layout = computePanelLayout(terminalRows, processes.length);
  const maxVisibleProcesses = layout.maxVisibleProcesses;
  const maxPreviewLines = layout.maxPreviewLines;

  const dim = (s: string) => theme.fg("dim", s);
  const accent = (s: string) => theme.fg("accent", s);
  const warning = (s: string) => theme.fg("warning", s);

  const innerWidth = width - 2;
  const basePadLine = createPanelPadder(width);
  const padLine = (content: string): string =>
    basePadLine(
      visibleWidth(content) > innerWidth
        ? truncateToWidth(content, innerWidth, "", true)
        : content,
    );

  const topLines: string[] = [];
  const processLines: string[] = [];

  topLines.push(renderPanelTitleLine("Tasks & Spawns", width, theme));

  if (processes.length === 0) {
    topLines.push(padLine(""));
    topLines.push(padLine(dim("No tasks or spawns")));
    topLines.push(padLine(dim("Start work with $, background_task, or meridian spawn")));
    topLines.push(padLine(""));
  } else {
    const selected = processes[model.selectedIndex];
    if (selected) {
      const output = host.getOutput(selected.id, maxPreviewLines * 2);
      topLines.push(padLine(""));
      const logTitle = `Output: ${accent(selected.name)} ${dim(`(${selected.id})`)}  ${dim("enter → stream")}`;
      topLines.push(padLine(truncateToWidth(logTitle, innerWidth, "", true)));

      const detailLines = formatTaskDetailLines(selected);
      for (const detailLine of detailLines) {
        topLines.push(padLine(warning(detailLine)));
      }
      if (detailLines.length > 0) {
        topLines.push(padLine(""));
      }

      let renderedLines = 0;
      model.logScroll.above = 0;
      model.logScroll.below = 0;

      if (output) {
        const logLines: { type: "stdout" | "stderr"; text: string }[] = [];
        for (const line of output.stdout) {
          logLines.push({ type: "stdout", text: line });
        }
        for (const line of output.stderr) {
          logLines.push({ type: "stderr", text: line });
        }

        if (logLines.length === 0) {
          topLines.push(padLine(dim("(no output yet)")));
          renderedLines = 1;
        } else {
          const logStart = Math.max(
            0,
            logLines.length - maxPreviewLines - model.logScrollOffset,
          );
          const logEnd = Math.max(0, logLines.length - model.logScrollOffset);
          const visibleLogLines = logLines.slice(logStart, logEnd);
          model.logScroll.above = logStart;
          model.logScroll.below =
            model.logScrollOffset > 0 ? logLines.length - logEnd : 0;

          for (const line of visibleLogLines) {
            const plain = stripAnsi(line.text);
            const displayLine =
              visibleWidth(plain) > innerWidth
                ? truncateToWidth(plain, innerWidth, "", true)
                : plain;
            topLines.push(
              padLine(line.type === "stderr" ? warning(displayLine) : displayLine),
            );
            renderedLines++;
          }
        }
      }

      while (renderedLines < maxPreviewLines) {
        topLines.push(padLine(""));
        renderedLines++;
      }
    }

    const columns = computePsColumnLayout(
      innerWidth,
      processes.length,
      maxVisibleProcesses,
      model.processScrollOffset,
    );

    processLines.push(renderPanelRule(width, theme));
    const header =
      "  " +
      fitCell(dim("Process"), columns.processWidth) +
      fitCell(dim("Command"), columns.cmdWidth) +
      fitCell(dim("Status"), columns.statusWidth) +
      fitCell(dim("Time"), columns.timeWidth) +
      fitCell(dim("Size"), columns.sizeWidth, "right") +
      (columns.headerSuffixText ? dim(columns.headerSuffixText) : "");
    processLines.push(padLine(header));
    processLines.push(renderPanelRule(width, theme));

    const visibleProcessCount = Math.min(maxVisibleProcesses, processes.length);
    const startIdx = model.processScrollOffset;
    const endIdx = startIdx + visibleProcessCount;

    for (let i = startIdx; i < endIdx; i++) {
      const proc = processes[i];
      if (!proc) {
        continue;
      }
      const isSelected = i === model.selectedIndex;
      const sizes = host.getFileSize(proc.id);
      const totalSize = sizes ? sizes.stdout + sizes.stderr : proc.logBytes;

      const statusText = formatStatus(theme, proc);
      const badge = kindBadge(proc);
      const idPlain = `(${proc.id})`;
      const maxNameLen = Math.max(
        1,
        columns.processWidth - visibleWidth(idPlain) - visibleWidth(badge) - 2,
      );
      const tName = truncate(proc.name, maxNameLen);
      const processCell = isSelected
        ? `${accent(`[${badge}]`)} ${accent(tName)} ${dim(idPlain)}`
        : `${dim(`[${badge}]`)} ${tName}${dim(` ${idPlain}`)}`;

      const row =
        fitCell(processCell, columns.processWidth) +
        fitCell(truncate(proc.command, columns.cmdWidth - 1), columns.cmdWidth) +
        fitCell(statusText, columns.statusWidth) +
        fitCell(formatRuntime(proc.startTime, proc.endTime), columns.timeWidth) +
        fitCell(formatBytes(totalSize), columns.sizeWidth, "right");

      processLines.push(padLine(isSelected ? `${accent(">")} ${row}` : `  ${row}`));
    }
  }

  const hasForeground = processes.some((proc) => proc.isForeground);
  const footerLeft =
    `${dim("enter")} stream  ` +
    `${dim("j/k")} select  ` +
    (hasForeground && !model.backgroundingForeground
      ? `${warning("b")} background  ${dim("/ps:b")}  `
      : "") +
    `${dim("x")} kill/cancel  ` +
    `${dim("c")} clear  ` +
    `${dim("q")} quit`;

  let footerRight = "";
  if (model.logScroll.above > 0 || model.logScroll.below > 0) {
    const parts: string[] = [];
    if (model.logScroll.above > 0) {
      parts.push(`↑${model.logScroll.above}`);
    }
    if (model.logScroll.below > 0) {
      parts.push(`↓${model.logScroll.below}`);
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

  const footerLines = [renderPanelRule(width, theme), padLine(footer)];
  const targetRows = Math.max(16, terminalRows - 1);
  const spacerCount = Math.max(
    0,
    targetRows - topLines.length - processLines.length - footerLines.length,
  );
  const spacerLines = Array.from({ length: spacerCount }, () => padLine(""));

  return [...topLines, ...spacerLines, ...processLines, ...footerLines];
}
