import { closeSync, openSync, writeSync } from "node:fs";

const LIFECYCLE_EVENT_FILE_ENV = "MERIDIAN_PI_LIFECYCLE_EVENT_FILE";

export type PiSessionRole = "primary" | "spawned";

export type LifecycleSidecarWriter = {
  append: (event: Record<string, unknown>) => void;
  close: () => void;
};

export function resolvePiSessionRole(): PiSessionRole {
  return process.env.MERIDIAN_PI_SESSION_ROLE === "spawned" ? "spawned" : "primary";
}

export function createLifecycleSidecarWriter(
  role: PiSessionRole = resolvePiSessionRole(),
): LifecycleSidecarWriter {
  const filePath = process.env[LIFECYCLE_EVENT_FILE_ENV]?.trim() ?? "";

  if (filePath.length === 0) {
    if (role === "spawned") {
      throw new Error(
        `${LIFECYCLE_EVENT_FILE_ENV} is required for spawned Pi lifecycle events`,
      );
    }
    return noopWriter();
  }

  let fd: number;
  try {
    fd = openSync(filePath, "a");
  } catch (error) {
    if (role === "spawned") {
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`failed to open lifecycle event file ${filePath}: ${reason}`);
    }
    return noopWriter();
  }

  let closed = false;

  return {
    append(event: Record<string, unknown>): void {
      if (closed) {
        return;
      }
      try {
        writeSync(fd, `${JSON.stringify(event)}\n`, undefined, "utf-8");
      } catch {
        // no stdout/stderr fallback for machine lifecycle events
      }
    },
    close(): void {
      if (closed) {
        return;
      }
      closed = true;
      try {
        closeSync(fd);
      } catch {
        // ignore close errors
      }
    },
  };
}

function noopWriter(): LifecycleSidecarWriter {
  return {
    append: () => undefined,
    close: () => undefined,
  };
}
