import type { ExtensionAPI, ToolCallEvent } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";

const POLICY_ENV = "_MERIDIAN_TOOL_POLICY";
const EVALUATOR_ENV = "_MERIDIAN_TOOL_EVALUATOR";
const DENIAL_PREFIX = "Blocked by Meridian policy: ";
// A decision is a Python start-up plus a linear pass; only a stuck process reaches this.
const EVALUATOR_TIMEOUT_MS = 10_000;

type ToolAction = "allow" | "ask" | "deny";

export type PermissionDecision = {
  action: ToolAction;
  detail: string;
  reason: string;
};

function parseEvaluatorArgv(raw: string | undefined): string[] {
  // Meridian adds this extension to a launch only when it has a policy to hand it, so a
  // missing variable means the bundle and the launcher disagree about its name. Loading
  // without an evaluator would run Pi with no permission checks.
  if (!process.env[POLICY_ENV]) throw new Error(`Pi permission policy is missing: ${POLICY_ENV} is not set`);
  if (!raw) throw new Error(`Pi permission evaluator is missing: ${EVALUATOR_ENV} is not set`);
  const value: unknown = JSON.parse(raw);
  if (!Array.isArray(value) || value.length === 0 || value.some((part) => typeof part !== "string")) {
    throw new Error("Pi permission evaluator is not a command line");
  }
  return value as string[];
}

function parseDecision(stdout: string): PermissionDecision {
  const value: unknown = JSON.parse(stdout);
  const candidate = value as Partial<PermissionDecision> | null;
  if (
    typeof candidate !== "object"
    || candidate === null
    || !["allow", "ask", "deny"].includes(String(candidate.action))
    || typeof candidate.detail !== "string"
    || typeof candidate.reason !== "string"
  ) {
    throw new Error("the evaluator answered with an unknown shape");
  }
  return candidate as PermissionDecision;
}

// Asks the evaluator about one call. Any failure to get an answer rejects.
export function evaluateToolCall(
  argv: string[],
  tool: string,
  input: Record<string, unknown>,
  cwd = process.cwd(),
): Promise<PermissionDecision> {
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0], [...argv.slice(1), "--json"], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
      timeout: EVALUATOR_TIMEOUT_MS,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf-8").on("data", (chunk: string) => (stdout += chunk));
    child.stderr.setEncoding("utf-8").on("data", (chunk: string) => (stderr += chunk));
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (code !== 0) {
        reject(new Error(`evaluator ${signal ?? `exited ${code}`}${stderr ? `: ${stderr.trim()}` : ""}`));
        return;
      }
      try {
        resolve(parseDecision(stdout));
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.on("error", () => undefined);
    child.stdin.end(JSON.stringify({ tool_name: tool, tool_input: input, cwd }));
  });
}

export default function meridianPermissionsExtension(pi: ExtensionAPI): void {
  const argv = parseEvaluatorArgv(process.env[EVALUATOR_ENV]);

  pi.on("tool_call", async (event: ToolCallEvent, ctx) => {
    const input = event.input as unknown as Record<string, unknown>;
    let decision: PermissionDecision;
    try {
      decision = await evaluateToolCall(argv, event.toolName, input);
    } catch (error) {
      return {
        block: true,
        reason: `${DENIAL_PREFIX}the tool policy could not be evaluated (${(error as Error).message}).`,
      };
    }
    if (decision.action === "allow") return undefined;

    let reason = decision.reason;
    if (decision.action === "ask") {
      // Outside a primary TUI nobody can be asked, and `reason` already says so.
      if (process.env.MERIDIAN_SESSION_ROLE === "primary" && ctx.mode === "tui") {
        if (await ctx.ui.confirm(`Allow ${event.toolName}?`, decision.detail)) return undefined;
        reason = "Denied by the user.";
      }
    }
    return { block: true, reason };
  });
}
