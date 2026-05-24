import { MAX_COMMAND_LENGTH, MAX_FOREGROUND_TAIL_BYTES } from "./task_constants";

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function toInt(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  return fallback;
}

export function truncateUtf8Tail(input: string, maxBytes: number): { text: string; truncated: boolean } {
  const buffer = Buffer.from(input, "utf-8");
  if (buffer.byteLength <= maxBytes) {
    return { text: input, truncated: false };
  }
  return {
    text: buffer.subarray(buffer.byteLength - maxBytes).toString("utf-8"),
    truncated: true,
  };
}

export function trimCombinedTails(stdoutTail: string, stderrTail: string): {
  stdoutTail: string;
  stderrTail: string;
  outputTruncated: boolean;
} {
  const stdoutBytes = Buffer.byteLength(stdoutTail, "utf-8");
  const stderrBytes = Buffer.byteLength(stderrTail, "utf-8");
  const total = stdoutBytes + stderrBytes;
  if (total <= MAX_FOREGROUND_TAIL_BYTES) {
    return {
      stdoutTail,
      stderrTail,
      outputTruncated: false,
    };
  }

  const half = Math.floor(MAX_FOREGROUND_TAIL_BYTES / 2);
  const stdoutMax = Math.max(0, MAX_FOREGROUND_TAIL_BYTES - Math.min(stderrBytes, half));
  const stderrMax = Math.max(0, MAX_FOREGROUND_TAIL_BYTES - Math.min(stdoutBytes, half));
  const stdout = truncateUtf8Tail(stdoutTail, stdoutMax).text;
  const stderr = truncateUtf8Tail(stderrTail, stderrMax).text;

  return {
    stdoutTail: stdout,
    stderrTail: stderr,
    outputTruncated: true,
  };
}

export function truncateCommand(command: string): string {
  const { text } = truncateUtf8Tail(command, MAX_COMMAND_LENGTH);
  return text;
}

export function nowMs(): number {
  return Date.now();
}

export function makeId(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}-${nowMs().toString(36)}-${rand}`;
}

export function normalizeWaitPolicy(value: unknown): import("./types").WaitPolicy {
  return value === "detached" ? "detached" : "tracked";
}

export function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export function killProcessTree(pid: number): void {
  if (process.platform === "win32") {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // ignore
    }
    return;
  }

  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // ignore
    }
  }
}

export async function killProcessTreeHard(pid: number): Promise<void> {
  if (process.platform === "win32") {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ignore
    }
    return;
  }

  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ignore
    }
  }
}
