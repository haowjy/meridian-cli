import { spawn, execFile, type ChildProcess } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);
const TERM_WAIT_MS = 500;
const KILL_WAIT_MS = 1500;
const delay = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** A fresh detached child establishes the only process group we ever signal. */
export class ShellTask {
  readonly child: ChildProcess;
  readonly closed: Promise<number>;
  private exitCode: number | null = null;
  private terminating: Promise<void> | null = null;

  constructor(command: string, cwd: string, env: NodeJS.ProcessEnv) {
    this.child = spawn(command, { cwd, env, shell: true, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    this.closed = new Promise((resolve) => {
      this.child.once("error", () => { this.exitCode = -1; resolve(-1); });
      this.child.once("close", (code) => resolve(code ?? -1));
    });
    this.child.once("exit", (code) => { this.exitCode = code ?? -1; });
  }

  async waitForExit(): Promise<number> {
    const code = await this.closed;
    // A shell can exit after launching background work with redirected pipes.
    // Keep ownership until that work exits too; zombies execute no further work.
    while (await this.hasLiveGroup()) await delay(50);
    return code;
  }

  terminate(): Promise<void> {
    if (!this.terminating) this.terminating = this.terminateGroup();
    return this.terminating;
  }

  private async terminateGroup(): Promise<void> {
    this.signal("SIGTERM");
    if (await this.waitUntilGone(TERM_WAIT_MS)) return;
    this.signal("SIGKILL");
    if (!await this.waitUntilGone(KILL_WAIT_MS)) throw new Error("Owned shell process group did not exit after SIGKILL");
  }

  private signal(signal: NodeJS.Signals): void {
    const pid = this.child.pid;
    if (pid == null) return;
    try { process.kill(-pid, signal); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; }
  }

  private async waitUntilGone(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    do {
      if (this.exitCode !== null && !await this.hasLiveGroup()) return true;
      await delay(25);
    } while (Date.now() < deadline);
    return this.exitCode !== null && !await this.hasLiveGroup();
  }

  private async hasLiveGroup(): Promise<boolean> {
    const pid = this.child.pid;
    if (pid == null) return false;
    try { process.kill(-pid, 0); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") return false;
      throw error;
    }
    // POSIX ps works on both supported platforms; never infer identity from a
    // persisted PID. This is only the group created by this live ShellTask.
    const { stdout } = await exec("ps", ["-A", "-o", "pgid=", "-o", "stat="], { timeout: 1000, maxBuffer: 4 * 1024 * 1024 });
    return stdout.split("\n").some((line) => {
      const [group, state] = line.trim().split(/\s+/);
      return Number(group) === pid && state != null && !state.startsWith("Z");
    });
  }
}
