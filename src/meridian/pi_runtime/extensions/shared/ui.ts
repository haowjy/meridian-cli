import { truncateToWidth } from "@earendil-works/pi-tui";

export type TableColumn<Row> = {
  header: string;
  width: number;
  render: (row: Row) => string;
};

export function formatDurationSecs(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "-";
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins < 60) return `${mins}m${secs.toString().padStart(2, "0")}s`;
  const hours = Math.floor(mins / 60);
  return `${hours}h${(mins % 60).toString().padStart(2, "0")}m`;
}

export function firstLine(value: string | null | undefined): string {
  return (value ?? "").split(/\r?\n/, 1)[0] ?? "";
}

export function renderTable<Row>(columns: TableColumn<Row>[], rows: Row[], width: number): string[] {
  const line = (cells: string[]): string =>
    truncateToWidth(
      cells.map((cell, idx) => truncateToWidth(cell, columns[idx]?.width ?? 10, "…").padEnd(columns[idx]?.width ?? 10)).join("  "),
      width,
      "…",
    );

  return [
    line(columns.map((column) => column.header)),
    line(columns.map((column) => "─".repeat(column.width))),
    ...rows.map((row) => line(columns.map((column) => column.render(row)))),
  ];
}
