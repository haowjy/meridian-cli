import { Type } from "typebox";
import { Value } from "typebox/value";

import { runMeridianCommand, type CommandResult } from "../../shared/meridian_cli";
import type { BashWaitResult } from "./bash_runtime";

// The CLI's single-ID wait projection is sparse, but present fields must be valid.
const WaitWire = Type.Object({
  any_failed: Type.Boolean(),
  checkpoint: Type.Optional(Type.Boolean()),
  checkpoint_pending_ids: Type.Optional(Type.Array(Type.String())),
  spawns: Type.Array(Type.Object({
    spawn_id: Type.String(),
    status: Type.String(),
    exit_code: Type.Optional(Type.Union([Type.Integer(), Type.Null()])),
    duration_secs: Type.Optional(Type.Number({ minimum: 0, maximum: Number.MAX_VALUE })),
    report_body: Type.Optional(Type.String()),
    report_summary: Type.Optional(Type.String()),
    failure_reason: Type.Optional(Type.String()),
  }), { minItems: 1, maxItems: 1 }),
});
const CommandError = Type.Object({ error: Type.String() });
const FAILED = new Set(["failed", "cancelled", "timed_out"]);
const ACTIVE = new Set(["queued", "running", "finalizing"]);

export async function waitForSpawn(spawnId: string, timeoutMin: number): Promise<BashWaitResult> {
  const seconds = timeoutMin * 60;
  // A spent wait budget is a clean checkpoint, not an unstructured CLI timeout.
  const result = await runMeridianCommand(
    ["--format", "json", "spawn", "wait", spawnId, "--yield-after-secs", String(seconds), "--full", "--quiet"],
    (seconds + 5) * 1000,
  );
  return parseSpawnWaitResult(spawnId, result);
}

function parseSpawnWaitResult(spawnId: string, result: CommandResult): BashWaitResult {
  const fault = (reason: string): BashWaitResult => ({
    bash_id: spawnId, status: "error", error: `spawn wait query failed for ${spawnId}: ${reason}`,
  });
  if (result.error) return fault(result.error);
  if (result.exitCode === null) return fault("CLI ended without an exit code");

  let payload: unknown;
  try { payload = JSON.parse(result.stdout); }
  catch { return fault(`invalid JSON${result.stderr.trim() ? `: ${result.stderr.trim()}` : ""}`); }
  if (Value.Check(CommandError, payload)) return fault(payload.error);
  if (!Value.Check(WaitWire, payload)) return fault("invalid structured result");

  const target = payload.spawns[0]!;
  if (target.spawn_id !== spawnId) return fault("response names a different child");
  const pending = ACTIVE.has(target.status);
  const failed = FAILED.has(target.status);
  if (!pending && !failed && target.status !== "succeeded") return fault(`unknown target status: ${target.status}`);
  if (payload.any_failed !== failed || (target.status === "succeeded" && (target.exit_code ?? 0) !== 0)) {
    return fault("inconsistent target outcome");
  }
  const pendingIds = payload.checkpoint_pending_ids ?? [];
  if (payload.checkpoint && (pendingIds.length !== (pending ? 1 : 0) || (pending && pendingIds[0] !== spawnId))) {
    return fault("inconsistent checkpoint membership");
  }

  return {
    bash_id: spawnId,
    status: target.status,
    exit_code: target.exit_code ?? null,
    duration_secs: target.duration_secs,
    output: target.report_body ?? target.report_summary ?? "",
    ...(failed ? { error: target.failure_reason || `spawn ${spawnId} ${target.status}` } : {}),
    ...(pending ? {
      checkpoint: payload.checkpoint === true,
      pending_ids: [spawnId],
      message: payload.checkpoint
        ? `Wait checkpoint for ${spawnId}; still ${target.status}. Wait again when ready.`
        : `Spawn ${spawnId} is still ${target.status}.`,
    } : {}),
  };
}
