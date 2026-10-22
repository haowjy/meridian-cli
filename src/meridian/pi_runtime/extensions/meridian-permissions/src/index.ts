import type {
  ExtensionAPI,
  ToolCallEvent,
} from "@earendil-works/pi-coding-agent";

const POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY";
const DENIAL_PREFIX = "Blocked by Meridian policy: ";

type ToolAction = "allow" | "ask" | "deny";

export type PermissionRule = {
  capability: string;
  tool: string;
  pattern?: string | null;
  action: ToolAction;
  label: string;
};

export type PermissionPolicy = {
  version: 1;
  rules: PermissionRule[];
  allowlist_tools: string[];
  completion_commands: string[];
  headless_reject_reason: string;
};

export type PermissionDecision = {
  action: ToolAction;
  detail: string;
  reason: string;
};

type RankedDecision = PermissionDecision & {
  rank: readonly [number, number, number, number];
};

type SplitResult = { commands: string[]; complete: boolean };

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function parsePolicy(raw: string | undefined): PermissionPolicy | undefined {
  if (!raw?.trim()) return undefined;
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || value === null) throw new Error("Pi permission policy is not an object");
  const policy = value as Partial<PermissionPolicy>;
  if (
    policy.version !== 1
    || !Array.isArray(policy.rules)
    || !isStringArray(policy.allowlist_tools)
    || !isStringArray(policy.completion_commands)
    || typeof policy.headless_reject_reason !== "string"
    || !policy.headless_reject_reason.trim()
  ) {
    throw new Error("Pi permission policy has an unsupported shape");
  }
  for (const rule of policy.rules) {
    if (
      typeof rule !== "object"
      || rule === null
      || typeof rule.capability !== "string"
      || typeof rule.tool !== "string"
      || (rule.pattern !== undefined && rule.pattern !== null && typeof rule.pattern !== "string")
      || !["allow", "ask", "deny"].includes(rule.action)
      || typeof rule.label !== "string"
    ) {
      throw new Error("Pi permission policy contains an invalid rule");
    }
  }
  return policy as PermissionPolicy;
}

function globMatches(pattern: string, value: string): boolean {
  if (pattern.endsWith(" *") && value === pattern.slice(0, -2)) return true;
  const expression = pattern
    .split("*")
    .map((part) => part.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${expression}$`, "u").test(value);
}

function literalLength(pattern: string | null | undefined): number {
  return pattern?.replaceAll("*", "").length ?? 0;
}

function actionRank(action: ToolAction): number {
  return action === "deny" ? 2 : action === "ask" ? 1 : 0;
}

function capabilityRank(rule: PermissionRule, tool: string): number {
  return tool === "write" && rule.capability === "write" ? 1 : 0;
}

function isHigherRank(left: RankedDecision, right: RankedDecision): boolean {
  for (let index = 0; index < left.rank.length; index += 1) {
    if (left.rank[index] !== right.rank[index]) return left.rank[index] > right.rank[index];
  }
  return false;
}

function policyDecision(
  policy: PermissionPolicy,
  tool: string,
  scope: string | undefined,
  detail: string,
): PermissionDecision | undefined {
  const candidates = policy.rules.filter((rule) => rule.tool === tool || rule.tool === "*");
  const scoped = candidates.filter((rule) => rule.pattern != null);
  if (scope === undefined && scoped.length > 0) {
    return {
      action: "deny",
      detail,
      reason: `${DENIAL_PREFIX}the scope for ${tool} could not be evaluated.`,
    };
  }

  let winner: RankedDecision | undefined;
  for (const rule of candidates) {
    if (rule.pattern != null && (scope === undefined || !globMatches(rule.pattern, scope))) continue;
    const decision: RankedDecision = {
      action: rule.action,
      detail,
      reason: `${DENIAL_PREFIX}${rule.label} = ${rule.action}. Attempted: ${detail}`,
      rank: [
        rule.pattern == null ? (rule.tool === "*" ? 0 : 1) : 2,
        literalLength(rule.pattern),
        capabilityRank(rule, tool),
        actionRank(rule.action),
      ],
    };
    if (winner === undefined || isHigherRank(decision, winner)) winner = decision;
  }
  if (winner !== undefined) return winner;
  if (policy.allowlist_tools.includes(tool)) {
    return {
      action: "deny",
      detail,
      reason: `${DENIAL_PREFIX}${tool} allows only its scoped rules.`,
    };
  }
  return undefined;
}

function matchingClose(source: string, opening: number): number | undefined {
  let depth = 1;
  let quote: "'" | '"' | null = null;
  let escaped = false;
  for (let index = opening + 1; index < source.length; index += 1) {
    const character = source[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (character === "\\" && quote !== "'") {
      escaped = true;
      continue;
    }
    if (character === "'" && quote !== '"') {
      quote = quote === "'" ? null : "'";
      continue;
    }
    if (character === '"' && quote !== "'") {
      quote = quote === '"' ? null : '"';
      continue;
    }
    if (quote !== null) continue;
    if (character === "(") depth += 1;
    if (character === ")") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return undefined;
}

function splitShellInto(source: string, commands: string[]): boolean {
  let current = "";
  let quote: "'" | '"' | null = null;
  let escaped = false;
  const flush = (): void => {
    const command = current.trim();
    if (command) commands.push(command);
    current = "";
  };

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (escaped) {
      current += character;
      escaped = false;
      continue;
    }
    if (character === "\\" && quote !== "'") {
      current += character;
      escaped = true;
      continue;
    }
    if (character === "'" && quote !== '"') {
      quote = quote === "'" ? null : "'";
      current += character;
      continue;
    }
    if (character === '"' && quote !== "'") {
      quote = quote === '"' ? null : '"';
      current += character;
      continue;
    }
    if (character === "$" && source[index + 1] === "(" && quote !== "'") {
      const close = matchingClose(source, index + 1);
      if (close === undefined) return false;
      if (!splitShellInto(source.slice(index + 2, close), commands)) return false;
      current += " substitution";
      index = close;
      continue;
    }
    if (character === "`" && quote !== "'") {
      const close = source.indexOf("`", index + 1);
      if (close < 0 || !splitShellInto(source.slice(index + 1, close), commands)) return false;
      current += " substitution";
      index = close;
      continue;
    }
    if (quote !== null) {
      current += character;
      continue;
    }
    if (character === ";" || character === "\n" || character === "|" || character === "&") {
      flush();
      if ((character === "|" || character === "&") && source[index + 1] === character) index += 1;
      continue;
    }
    if (character === "(" || character === ")") {
      flush();
      continue;
    }
    current += character;
  }
  if (quote !== null || escaped) return false;
  flush();
  return true;
}

export function splitShellCommands(source: string): SplitResult {
  const commands: string[] = [];
  return { commands, complete: splitShellInto(source, commands) };
}

function scopeValue(tool: string, input: Record<string, unknown>): string | undefined {
  if (tool === "bash") return typeof input.command === "string" ? input.command : undefined;
  if (["read", "edit", "write"].includes(tool)) {
    return typeof input.path === "string" ? input.path : undefined;
  }
  if (["grep", "find", "ls"].includes(tool)) {
    return typeof input.path === "string" ? input.path : ".";
  }
  return undefined;
}

function callDetail(tool: string, input: Record<string, unknown>): string {
  return scopeValue(tool, input) ?? tool;
}

function isCompletionCommand(policy: PermissionPolicy, command: string): boolean {
  return policy.completion_commands.some(
    (candidate) => command === candidate || command.startsWith(`${candidate} `),
  );
}

export function decideToolCall(
  policy: PermissionPolicy,
  tool: string,
  input: Record<string, unknown>,
): PermissionDecision | undefined {
  const scope = scopeValue(tool, input);
  const detail = callDetail(tool, input);
  if (tool !== "bash") return policyDecision(policy, tool, scope, detail);

  const bashIsRestricted = policy.allowlist_tools.includes("bash")
    || policy.rules.some((rule) => rule.tool === "bash" || rule.tool === "*");
  if (scope === undefined) {
    return bashIsRestricted
      ? { action: "deny", detail, reason: `${DENIAL_PREFIX}the bash command could not be evaluated.` }
      : undefined;
  }

  const split = splitShellCommands(scope);
  if (!split.complete || split.commands.length === 0) {
    return bashIsRestricted
      ? { action: "deny", detail, reason: `${DENIAL_PREFIX}the bash command could not be evaluated.` }
      : undefined;
  }

  const decisions = split.commands.map((command) => (
    isCompletionCommand(policy, command)
      ? { action: "allow" as const, detail, reason: "Meridian completion command" }
      : policyDecision(policy, tool, command, detail)
  ));
  return decisions.find((decision) => decision?.action === "deny")
    ?? decisions.find((decision) => decision?.action === "ask")
    ?? decisions.find((decision) => decision?.action === "allow");
}

export default function meridianPermissionsExtension(pi: ExtensionAPI): void {
  const policy = parsePolicy(process.env[POLICY_ENV]);
  if (policy === undefined) return;

  pi.on("tool_call", async (event: ToolCallEvent, ctx) => {
    const input = event.input as unknown as Record<string, unknown>;
    const decision = decideToolCall(policy, event.toolName, input);
    if (decision === undefined || decision.action === "allow") return undefined;

    let reason = decision.reason;
    if (decision.action === "ask") {
      if (process.env.MERIDIAN_SESSION_ROLE !== "primary" || ctx.mode !== "tui") {
        reason = policy.headless_reject_reason;
      } else if (await ctx.ui.confirm(`Allow ${event.toolName}?`, decision.detail)) {
        return undefined;
      } else {
        reason = "Denied by the user.";
      }
    }
    return { block: true, reason };
  });
}
