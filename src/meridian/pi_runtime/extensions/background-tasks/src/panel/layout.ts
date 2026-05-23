/** Split terminal height between process list and log preview (no fixed 8+12 padding). */
export function computePanelLayout(
  terminalRows: number,
  processCount: number,
): { maxVisibleProcesses: number; maxPreviewLines: number } {
  const rows = Math.max(20, terminalRows);
  const chromeLines = 11;
  const remaining = Math.max(6, rows - chromeLines);
  const cappedCount = Math.max(1, Math.min(processCount, 8));
  const processLines = Math.min(cappedCount, Math.max(1, Math.floor(remaining * 0.28)));
  const logLines = Math.max(3, remaining - processLines);
  return {
    maxVisibleProcesses: processLines,
    maxPreviewLines: logLines,
  };
}
