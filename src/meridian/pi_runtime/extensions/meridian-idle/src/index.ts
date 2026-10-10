import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

import { runMeridianCommand, type CommandResult } from "../../shared/meridian_cli";

export type MeridianRunner = (
  args: string[],
  timeoutMs?: number,
) => Promise<CommandResult>;

type IdleStage = "push" | "warn" | "compact";
type ActiveStretch = {
  session: string;
  stretch: number;
  anchor: number;
  ctx: ExtensionContext;
};
type ArmReply = {
  stretch?: unknown;
  anchor?: unknown;
  push_at?: unknown;
  warn_at?: unknown;
  compact_at?: unknown;
  reason?: unknown;
};
type StatusRow = {
  harness?: unknown;
  session?: unknown;
  stretch?: unknown;
  anchor?: unknown;
  schedule?: unknown;
  done?: unknown;
};

const STAGES: readonly IdleStage[] = ["push", "warn", "compact"];
const MAX_TIMER_DELAY_MS = 2_147_483_647;
const USER_EXCERPT_CHARS = 120;
const ASSISTANT_EXCERPT_CHARS = 280;

function trimExcerpt(value: string, limit: number): string | null {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return null;
  if (normalized.length <= limit) return normalized;
  const prefix = normalized.slice(0, limit - 1);
  const boundary = prefix.lastIndexOf(" ");
  return `${(boundary > 0 ? prefix.slice(0, boundary) : prefix).trimEnd()}…`;
}

function messageText(content: unknown): string | null {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  const parts = content
    .filter((part): part is Record<string, unknown> => isRecord(part))
    .filter(part => part.type === "text" && typeof part.text === "string")
    .map(part => String(part.text));
  return parts.length > 0 ? parts.join(" ") : null;
}

export function messageExcerpts(messages: unknown): {
  user: string | null;
  assistant: string | null;
} {
  let user: string | null = null;
  let assistant: string | null = null;
  if (!Array.isArray(messages)) return { user, assistant };
  for (const message of messages) {
    if (!isRecord(message)) continue;
    const text = messageText(message.content);
    if (text === null) continue;
    if (message.role === "user") user = trimExcerpt(text, USER_EXCERPT_CHARS);
    if (message.role === "assistant") {
      assistant = trimExcerpt(text, ASSISTANT_EXCERPT_CHARS);
    }
  }
  return { user, assistant };
}

export class IdleRuntime {
  private enabled = false;
  private revision = 0;
  private active: ActiveStretch | null = null;
  private readonly timers = new Map<IdleStage, NodeJS.Timeout>();
  private readonly deadlines = new Map<IdleStage, number>();
  private transitionQueue: Promise<void> = Promise.resolve();

  constructor(private readonly run: MeridianRunner = runMeridianCommand) {}

  async start(ctx: ExtensionContext): Promise<void> {
    const revision = ++this.revision;
    this.enabled = false;
    this.clearSchedule();
    this.active = null;

    const config = await this.runJson(["idle", "config"]);
    if (revision !== this.revision || !isRecord(config) || config.enabled !== true) {
      return;
    }

    this.enabled = true;
    const rows = await this.runJson(["idle", "status", "--json"]);
    if (revision !== this.revision || !Array.isArray(rows)) {
      return;
    }
    this.restore(rows, ctx);
  }

  async agentEnd(ctx: ExtensionContext, messages?: unknown): Promise<void> {
    if (!this.enabled || !safeIsIdle(ctx)) {
      return;
    }

    const revision = this.revision;
    await this.enqueueTransition(async () => {
      if (revision !== this.revision || !this.enabled) {
        return;
      }

      const previous = this.active;
      const previousDeadlines = new Map(this.deadlines);
      this.clearTimers();

      const session = ctx.sessionManager.getSessionId();
      const args = [
        "idle", "arm", "--harness", "pi", "--session", session,
      ];
      const provider = ctx.model?.provider;
      if (provider) {
        args.push("--provider", provider);
      }
      args.push("--cwd", ctx.cwd);
      const excerpts = messageExcerpts(messages);
      if (excerpts.user !== null) args.push("--user-text", excerpts.user);
      if (excerpts.assistant !== null) {
        args.push("--assistant-text", excerpts.assistant);
      }

      const reply = await this.runJson(args);
      if (revision !== this.revision) {
        return;
      }
      if (!isRecord(reply)) {
        this.restorePrevious(previous, previousDeadlines);
        return;
      }
      this.applyArmReply(reply, ctx, previous, previousDeadlines);
    });
  }

  input(source: string, ctx: ExtensionContext): void {
    if (!this.enabled || source !== "interactive") {
      return;
    }

    ++this.revision;
    this.clearSchedule();
    this.active = null;
    const session = ctx.sessionManager.getSessionId();
    void this.enqueueTransition(async () => {
      await this.runJson([
        "idle", "return", "--harness", "pi",
        "--session", session, "--user-prompt",
      ]);
    });
  }

  stop(): void {
    ++this.revision;
    this.enabled = false;
    this.clearSchedule();
    this.active = null;
  }

  private restore(rows: unknown[], ctx: ExtensionContext): void {
    const session = ctx.sessionManager.getSessionId();
    const row = rows.find((candidate): candidate is StatusRow => (
      isRecord(candidate)
      && candidate.harness === "pi"
      && candidate.session === session
    ));
    if (!row) {
      return;
    }

    const stretch = integer(row.stretch);
    const anchor = integer(row.anchor);
    if (stretch === null || anchor === null || !isRecord(row.schedule)) {
      return;
    }

    this.active = { session, stretch, anchor, ctx };
    const done = isRecord(row.done) ? row.done : {};
    for (const stage of STAGES) {
      if (!(stage in done)) {
        this.setDeadline(stage, row.schedule[`${stage}_at`]);
      }
    }
  }

  private applyArmReply(
    reply: ArmReply,
    ctx: ExtensionContext,
    previous: ActiveStretch | null,
    previousDeadlines: Map<IdleStage, number>,
  ): void {
    const stretch = integer(reply.stretch);
    const anchor = integer(reply.anchor);
    if (stretch === null || anchor === null) {
      this.clearSchedule();
      this.active = null;
      return;
    }

    if (
      typeof reply.reason === "string"
      && previous?.stretch === stretch
      && previous.anchor === anchor
    ) {
      this.restorePrevious({ ...previous, ctx }, previousDeadlines);
      return;
    }

    this.clearSchedule();
    this.active = {
      session: ctx.sessionManager.getSessionId(),
      stretch,
      anchor,
      ctx,
    };
    for (const stage of STAGES) {
      this.setDeadline(stage, reply[`${stage}_at`]);
    }
  }

  private restorePrevious(
    previous: ActiveStretch | null,
    previousDeadlines: Map<IdleStage, number>,
  ): void {
    this.active = previous;
    this.deadlines.clear();
    for (const [stage, deadline] of previousDeadlines) {
      this.deadlines.set(stage, deadline);
      this.armTimer(stage, deadline);
    }
  }

  private setDeadline(stage: IdleStage, value: unknown): void {
    const deadline = integer(value);
    if (deadline === null) {
      return;
    }
    this.deadlines.set(stage, deadline);
    this.armTimer(stage, deadline);
  }

  private armTimer(stage: IdleStage, deadline: number): void {
    const delay = Math.min(
      MAX_TIMER_DELAY_MS,
      Math.max(0, deadline - Date.now()),
    );
    const timer = setTimeout(() => {
      if (this.deadlines.get(stage) !== deadline) {
        return;
      }
      if (Date.now() < deadline) {
        this.armTimer(stage, deadline);
        return;
      }
      this.timers.delete(stage);
      this.deadlines.delete(stage);
      const active = this.active;
      if (active) {
        void this.fire(stage, active);
      }
    }, delay);
    timer.unref();
    this.timers.set(stage, timer);
  }

  private async fire(stage: IdleStage, active: ActiveStretch): Promise<void> {
    const revision = this.revision;
    const args = [
      "idle", "fire", stage,
      "--harness", "pi",
      "--session", active.session,
      "--stretch", String(active.stretch),
      "--anchor", String(active.anchor),
      "--draft", draftFact(active.ctx),
    ];
    if (busyFact(active.ctx)) {
      args.push("--busy");
    }
    const tokens = contextTokens(active.ctx);
    if (tokens !== null) {
      args.push("--context-tokens", String(tokens));
    }

    const result = await this.runJson(args);
    if (stage !== "compact" || !isRecord(result) || result.decision !== "act") {
      return;
    }

    const veto = this.compactionVeto(active.ctx, revision);
    if (veto !== null) {
      await this.done(active, "vetoed", veto);
      return;
    }

    try {
      active.ctx.compact({
        onComplete: () => {
          void this.done(active, "ok");
        },
        onError: (error) => {
          void this.done(active, "failed", String(error));
        },
      });
    } catch (error) {
      await this.done(active, "failed", String(error));
    }
  }

  private async done(
    active: ActiveStretch,
    result: "ok" | "failed" | "vetoed",
    reason?: string,
  ): Promise<void> {
    const args = [
      "idle", "done", "compact",
      "--harness", "pi",
      "--session", active.session,
      "--stretch", String(active.stretch),
      "--result", result,
    ];
    if (reason) {
      args.push("--reason", reason);
    }
    await this.runJson(args);
  }

  private compactionVeto(ctx: ExtensionContext, revision: number): string | null {
    if (revision !== this.revision) {
      return "user returned";
    }
    if (!safeIsIdle(ctx)) {
      return "busy";
    }
    if (hasPendingMessages(ctx)) {
      return "pending messages";
    }
    if (draftFact(ctx) !== "no") {
      return "draft";
    }
    return null;
  }

  private enqueueTransition(task: () => Promise<void>): Promise<void> {
    const next = this.transitionQueue.then(task).catch(() => undefined);
    this.transitionQueue = next;
    return next;
  }

  private async runJson(args: string[]): Promise<unknown> {
    try {
      const result = await this.run([...args, "--interactive"]);
      if (result.exitCode !== 0) {
        return undefined;
      }
      return JSON.parse(result.stdout);
    } catch {
      return undefined;
    }
  }

  private clearTimers(): void {
    for (const timer of this.timers.values()) {
      clearTimeout(timer);
    }
    this.timers.clear();
  }

  private clearSchedule(): void {
    this.clearTimers();
    this.deadlines.clear();
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function safeIsIdle(ctx: ExtensionContext): boolean {
  try {
    return ctx.isIdle();
  } catch {
    return false;
  }
}

function hasPendingMessages(ctx: ExtensionContext): boolean {
  try {
    return ctx.hasPendingMessages();
  } catch {
    return true;
  }
}

function draftFact(ctx: ExtensionContext): "yes" | "no" | "unknown" {
  try {
    return ctx.ui.getEditorText().length > 0 ? "yes" : "no";
  } catch {
    return "unknown";
  }
}

function busyFact(ctx: ExtensionContext): boolean {
  try {
    return !ctx.isIdle() || ctx.hasPendingMessages();
  } catch {
    return true;
  }
}

function contextTokens(ctx: ExtensionContext): number | null {
  try {
    const tokens = ctx.getContextUsage()?.tokens;
    return typeof tokens === "number" && Number.isSafeInteger(tokens) ? tokens : null;
  } catch {
    return null;
  }
}

export function registerMeridianIdleExtension(
  pi: ExtensionAPI,
  run: MeridianRunner = runMeridianCommand,
): IdleRuntime {
  const runtime = new IdleRuntime(run);
  pi.on("session_start", async (_event, ctx) => runtime.start(ctx));
  pi.on("agent_end", (event, ctx) => {
    // Pi still reports streaming while it awaits agent_end handlers. Check on
    // the next event-loop turn, when ctx.isIdle() reflects the completed turn.
    const timer = setTimeout(() => {
      void runtime.agentEnd(ctx, event.messages);
    }, 0);
    timer.unref();
  });
  pi.on("input", (event, ctx) => runtime.input(event.source, ctx));
  pi.on("session_shutdown", () => runtime.stop());
  return runtime;
}

export default function meridianIdleExtension(pi: ExtensionAPI): void {
  registerMeridianIdleExtension(pi);
}
