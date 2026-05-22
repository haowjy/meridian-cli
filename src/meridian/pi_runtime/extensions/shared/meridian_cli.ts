import { spawn } from "node:child_process";

export type CommandResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
};

export async function runMeridianCommand(
  args: string[],
  timeoutMs = 8_000,
): Promise<CommandResult> {
  return await new Promise<CommandResult>((resolve) => {
    let stdout = "";
    let stderr = "";
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
      });
    };

    const timer = setTimeout(() => {
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
    child.once("error", () => {
      clearTimeout(timer);
      finalize();
    });
  });
}
