import { spawn } from "node:child_process";

export type CommandResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  /** Process-launch or timeout failures; a non-zero exit alone is target data. */
  error?: string;
};

export async function runMeridianCommand(
  args: string[],
  timeoutMs = 8_000,
  signal?: AbortSignal,
): Promise<CommandResult> {
  return await new Promise<CommandResult>((resolve) => {
    let stdout = "";
    let stderr = "";
    let errorMessage: string | undefined;
    let finished = false;

    const child = spawn("meridian", args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });

    const finalize = (): void => {
      if (finished) {
        return;
      }
      finished = true;
      resolve({
        stdout,
        stderr,
        exitCode: child.exitCode,
        ...(errorMessage ? { error: errorMessage } : {}),
      });
    };

    const timer = setTimeout(() => {
      errorMessage = `meridian ${args.join(" ")} timed out after ${timeoutMs}ms`;
      try {
        child.kill("SIGTERM");
      } catch {
        // ignore
      }
      finalize();
    }, Math.max(1, timeoutMs));

    child.stdout?.setEncoding("utf-8");
    child.stdout?.on("data", (chunk: string | Buffer) => {
      stdout += typeof chunk === "string" ? chunk : chunk.toString("utf-8");
    });
    child.stderr?.setEncoding("utf-8");
    child.stderr?.on("data", (chunk: string | Buffer) => {
      stderr += typeof chunk === "string" ? chunk : chunk.toString("utf-8");
    });
    child.once("close", () => {
      clearTimeout(timer);
      finalize();
    });
    child.once("error", (error) => {
      errorMessage = error.message;
      clearTimeout(timer);
      finalize();
    });
    const abort = (): void => {
      errorMessage = `meridian ${args.join(" ")} aborted`;
      clearTimeout(timer);
      child.kill("SIGTERM");
      finalize();
    };
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
    child.once("close", () => signal?.removeEventListener("abort", abort));
  });
}
