import type { BashRecord, BashStatus } from "./schemas";

/** Outcome evidence is independent of log contents: an empty log can accompany a failure. */
export type TaskOutcome = {
  status: string;
  exit_code?: number | null;
  error?: string;
};

export type BashOutcome = TaskOutcome & { status: BashStatus; exit_code: number | null };

/** The one Bash-to-result projection, used by tools and automatic delivery. */
export function bashOutcome(record: Pick<BashRecord, "status" | "exit_code" | "execution_error">): BashOutcome {
  return { status: record.status, exit_code: record.exit_code, ...(record.execution_error ? { error: record.execution_error } : {}) };
}

export function outcomeIsError(outcome: TaskOutcome): boolean {
  return Boolean(outcome.error) || outcome.status === "killed" || outcome.status === "timed_out"
    || outcome.status === "failed" || outcome.status === "cancelled" || outcome.status === "error"
    || (outcome.exit_code != null && outcome.exit_code !== 0);
}

export function describeOutcome(id: string, outcome: TaskOutcome): string {
  return `${id}: ${outcome.status}${outcome.exit_code == null ? "" : ` (exit code ${outcome.exit_code})`}`;
}
