import type {
  ExtensionAPI,
  ToolCallEvent,
} from "@earendil-works/pi-coding-agent";
import { lstatSync, readlinkSync, realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, parse, resolve, sep } from "node:path";

const POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY";
const DENIAL_PREFIX = "Blocked by Meridian policy: ";
const SIMPLE_COMMAND_REASON = `${DENIAL_PREFIX}restricted bash accepts only simple commands; run simple commands without expansion, redirection, or backgrounding.`;
const PATH_TOOLS = new Set(["read", "edit", "write", "grep", "find", "ls"]);
const SHELL_RESERVED_WORDS = new Set([
  "!",
  "[[",
  "]]",
  "case",
  "coproc",
  "do",
  "done",
  "elif",
  "else",
  "esac",
  "fi",
  "for",
  "function",
  "if",
  "in",
  "select",
  "then",
  "time",
  "until",
  "while",
]);

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
  meridian_commands: { command: string; match: "exact" | "prefix" }[];
  headless_reject_reason: string;
  // Refuse a bash command that does not split into simple commands. Meridian sets it when
  // such a command could be refused without matching a scoped rule (an allowlist, or an
  // unmatched-command default of deny or ask); otherwise only scoped denies can refuse, and
  // a command the splitter cannot read runs unchecked.
  bash_fail_closed: boolean;
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
type ShellWord = { value: string; quoted: boolean };

function isMeridianCommandGrant(
  value: unknown,
): value is { command: string; match: "exact" | "prefix" } {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { command?: unknown; match?: unknown };
  return typeof candidate.command === "string"
    && candidate.command.startsWith("meridian ")
    && ["exact", "prefix"].includes(String(candidate.match));
}

function parsePolicy(raw: string | undefined): PermissionPolicy | undefined {
  if (raw === undefined) return undefined;
  if (!raw.trim()) throw new Error("Pi permission policy is empty");
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || value === null) throw new Error("Pi permission policy is not an object");
  const policy = value as Partial<PermissionPolicy>;
  if (
    policy.version !== 1
    || !Array.isArray(policy.rules)
    || !Array.isArray(policy.meridian_commands)
    || typeof policy.headless_reject_reason !== "string"
    || !policy.headless_reject_reason.trim()
    || typeof policy.bash_fail_closed !== "boolean"
    || policy.meridian_commands.some((command) => !isMeridianCommandGrant(command))
  ) {
    throw new Error("Pi permission policy has an unsupported shape");
  }
  for (const rule of policy.rules) {
    if (
      typeof rule !== "object"
      || rule === null
      || typeof rule.capability !== "string"
      || !rule.capability.trim()
      || typeof rule.tool !== "string"
      || !rule.tool.trim()
      || (rule.pattern !== undefined && rule.pattern !== null && typeof rule.pattern !== "string")
      || (typeof rule.pattern === "string" && !rule.pattern.trim())
      || !["allow", "ask", "deny"].includes(rule.action)
      || typeof rule.label !== "string"
      || !rule.label.trim()
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
  workingDirectory: string,
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
    if (rule.pattern != null) {
      const pattern = PATH_TOOLS.has(tool)
        ? normalizePathPattern(rule.pattern, workingDirectory)
        : rule.pattern;
      if (scope === undefined || pattern === undefined || !globMatches(pattern, scope)) continue;
    }
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
  return winner;
}

function isAssignment(word: ShellWord): boolean {
  return /^[A-Za-z_][A-Za-z0-9_]*=/u.test(word.value);
}

function splitShellInto(source: string, commands: string[]): boolean {
  let current = "";
  let quote: "'" | '"' | null = null;
  let word = "";
  let wordStarted = false;
  let wordQuoted = false;
  let words: ShellWord[] = [];
  let commandRequired = false;
  const finishWord = (): void => {
    if (!wordStarted) return;
    words.push({ value: word, quoted: wordQuoted });
    word = "";
    wordStarted = false;
    wordQuoted = false;
  };
  const flush = (): boolean => {
    finishWord();
    const command = current.trim();
    if (!command) return false;
    const executable = words.find((candidate) => !isAssignment(candidate));
    if (executable !== undefined
      && !executable.quoted
      && SHELL_RESERVED_WORDS.has(executable.value)) {
      return false;
    }
    commands.push(command);
    current = "";
    words = [];
    commandRequired = false;
    return true;
  };

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quote === "'") {
      current += character;
      if (character === "'") quote = null;
      else word += character;
      continue;
    }
    if (quote === '"') {
      current += character;
      if (character === '"') {
        quote = null;
      } else if (character === "$" || character === "`" || character === "\\") {
        return false;
      } else {
        word += character;
      }
      continue;
    }
    if (character === "'" || character === '"') {
      quote = character;
      current += character;
      wordStarted = true;
      wordQuoted = true;
      commandRequired = false;
      continue;
    }
    if ("\\$`()<>#{}`".includes(character)) return false;
    if (character === "\r") return false;
    if (character === " " || character === "\t") {
      finishWord();
      current += character;
      continue;
    }
    if (character === "\n") {
      if (current.trim() && !flush()) return false;
      continue;
    }
    if (character === ";") {
      if (!flush()) return false;
      continue;
    }
    if (character === "|" || character === "&") {
      const doubled = source[index + 1] === character;
      if (character === "&" && !doubled) return false;
      if (!flush()) return false;
      if (doubled) index += 1;
      commandRequired = true;
      continue;
    }
    current += character;
    word += character;
    wordStarted = true;
    commandRequired = false;
  }
  if (quote !== null || commandRequired) return false;
  if (current.trim()) return flush();
  return commands.length > 0;
}

export function splitShellCommands(source: string): SplitResult {
  const commands: string[] = [];
  return { commands, complete: splitShellInto(source, commands) };
}

function canonicalPath(path: string): string | undefined {
  let cursor = path;
  const unresolved: string[] = [];
  let symlinksFollowed = 0;
  while (true) {
    try {
      return resolve(realpathSync.native(cursor), ...unresolved.reverse());
    } catch {
      try {
        if (lstatSync(cursor).isSymbolicLink()) {
          symlinksFollowed += 1;
          if (symlinksFollowed > 40) return undefined;
          cursor = anchorPath(readlinkSync(cursor), dirname(cursor));
          continue;
        }
      } catch {
        // Walk to the nearest existing ancestor below.
      }
      const parent = dirname(cursor);
      if (parent === cursor) return undefined;
      unresolved.push(basename(cursor));
      cursor = parent;
    }
  }
}

function anchorPath(path: string, workingDirectory: string): string {
  if (isAbsolute(path)) return path;
  const anchor = resolve(workingDirectory);
  return anchor.endsWith(sep) ? anchor + path : anchor + sep + path;
}

function normalizePathScope(path: string, workingDirectory: string): string | undefined {
  return canonicalPath(anchorPath(path, workingDirectory));
}

function normalizePathPattern(pattern: string, workingDirectory: string): string | undefined {
  const absolute = anchorPath(pattern, workingDirectory);
  const wildcard = absolute.indexOf("*");
  if (wildcard < 0) return canonicalPath(absolute);
  const boundary = absolute.lastIndexOf(sep, wildcard);
  const root = boundary <= 0 ? parse(absolute).root : absolute.slice(0, boundary);
  const canonicalRoot = canonicalPath(root);
  return canonicalRoot === undefined ? undefined : canonicalRoot + absolute.slice(boundary);
}

function rawScopeValue(tool: string, input: Record<string, unknown>): string | undefined {
  if (tool === "bash") return typeof input.command === "string" ? input.command : undefined;
  if (["read", "edit", "write"].includes(tool)) {
    return typeof input.path === "string" ? input.path : undefined;
  }
  if (["grep", "find", "ls"].includes(tool)) {
    return typeof input.path === "string" ? input.path : ".";
  }
  return undefined;
}

function scopeValue(
  tool: string,
  input: Record<string, unknown>,
  workingDirectory: string,
): string | undefined {
  const scope = rawScopeValue(tool, input);
  return scope !== undefined && PATH_TOOLS.has(tool)
    ? normalizePathScope(scope, workingDirectory)
    : scope;
}

function callDetail(tool: string, input: Record<string, unknown>): string {
  return rawScopeValue(tool, input) ?? tool;
}

function isMeridianCommand(policy: PermissionPolicy, command: string): boolean {
  return policy.meridian_commands.some((candidate) => candidate.match === "exact"
    ? command === candidate.command
    : command === candidate.command || command.startsWith(`${candidate.command} `));
}

export function decideToolCall(
  policy: PermissionPolicy,
  tool: string,
  input: Record<string, unknown>,
  workingDirectory = process.cwd(),
): PermissionDecision | undefined {
  const scope = scopeValue(tool, input, workingDirectory);
  const detail = callDetail(tool, input);
  if (tool !== "bash") return policyDecision(policy, tool, scope, detail, workingDirectory);

  if (scope === undefined) {
    return policy.bash_fail_closed
      ? { action: "deny", detail, reason: `${DENIAL_PREFIX}the bash command could not be evaluated.` }
      : undefined;
  }

  const split = splitShellCommands(scope);
  if (!split.complete || split.commands.length === 0) {
    return policy.bash_fail_closed
      ? { action: "deny", detail, reason: SIMPLE_COMMAND_REASON }
      : undefined;
  }

  if (split.commands.length === 1 && isMeridianCommand(policy, split.commands[0])) {
    return { action: "allow", detail, reason: "Meridian platform command" };
  }
  const decisions = split.commands.map(
    (command) => policyDecision(policy, tool, command, detail, workingDirectory),
  );
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
