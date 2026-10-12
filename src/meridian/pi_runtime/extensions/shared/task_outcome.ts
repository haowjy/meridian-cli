import type { BashRecord, BashStatus } from "./schemas";

/** Outcome evidence is independent of log contents: an empty log can accompany a failure. */
export type TaskOutcome = {
  status: string;
  exit_code?: number | null;
  error?: string;
};

export type BashOutcome = TaskOutcome & { status: BashStatus; exit_code: number | null };

const UNSPECIFIED_EXECUTION_FAILURE = "unspecified execution failure";
const MISSING_EXIT_EVIDENCE = "exit code unavailable";

/** The one Bash-to-result projection, used by tools and automatic delivery. */
export function bashOutcome(record: Pick<BashRecord, "status" | "exit_code" | "execution_error">): BashOutcome {
  const error = record.execution_error !== undefined
    ? (record.execution_error.trim() ? record.execution_error : UNSPECIFIED_EXECUTION_FAILURE)
    : record.status === "exited" && record.exit_code === null ? MISSING_EXIT_EVIDENCE : undefined;
  return { status: record.status, exit_code: record.exit_code, ...(error === undefined ? {} : { error }) };
}

/** Error presence is authoritative even when an upstream diagnostic has no text. */
export function outcomeErrorText(outcome: TaskOutcome): string | undefined {
  if (outcome.error === undefined) return undefined;
  return outcome.error.trim() ? outcome.error : UNSPECIFIED_EXECUTION_FAILURE;
}

export function outcomeIsError(outcome: TaskOutcome): boolean {
  return outcomeErrorText(outcome) !== undefined || outcome.status === "killed" || outcome.status === "aborted" || outcome.status === "timed_out"
    || outcome.status === "failed" || outcome.status === "cancelled" || outcome.status === "error"
    || (outcome.exit_code != null && outcome.exit_code !== 0);
}

export function describeOutcome(id: string, outcome: TaskOutcome): string {
  return `${id}: ${outcome.status}${outcome.exit_code == null ? "" : ` (exit code ${outcome.exit_code})`}`;
}
