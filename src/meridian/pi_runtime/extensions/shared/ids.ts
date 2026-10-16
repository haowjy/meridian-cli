/** Mirror of core/refs.py: user refs, not filesystem discovery. */
export type MeridianRef = {
  kind: "spawn" | "run" | "chat" | "native";
  value: string;
  spawnId?: string;
  runId?: string;
  run?: number;
  chatId?: string;
};

export function parseRef(text: string): MeridianRef {
  const value = text.trim();
  const match = /^p([0-9]+)(?:\.([0-9]+))?$/.exec(value);
  if (match && (match[2] === undefined || BigInt(match[2]) >= 1n)) {
    const spawnId = `p${match[1]}`;
    if (match[2] === undefined) {
      return { kind: "spawn", value, spawnId };
    }
    const run = BigInt(match[2]);
    return {
      kind: "run",
      value,
      spawnId,
      runId: run === 1n ? spawnId : `${spawnId}.${run}`,
      run: Number(run),
    };
  }
  if (/^c[0-9]+$/.test(value)) {
    return { kind: "chat", value, chatId: value };
  }
  // Pi extensions do not resolve CLI aliases; values such as @latest remain native here.
  return { kind: "native", value };
}

export function isBashId(value: string | undefined | null): value is string {
  return typeof value === "string" && /^b-[0-9a-f]{8}$/i.test(value);
}

/** A stable spawn reference such as p5, never an explicit run reference. */
export function isStableSpawnId(value: string | undefined | null): value is string {
  return typeof value === "string" && parseRef(value).kind === "spawn";
}

/** An explicit run reference such as p5.1 or p5.2, never a bare spawn. */
export function isExactRunId(value: string | undefined | null): value is string {
  return typeof value === "string" && parseRef(value).kind === "run";
}

export function classifyWorkId(value: string | undefined | null): "bash" | "spawn" | null {
  if (isBashId(value)) return "bash";
  if (isStableSpawnId(value) || isExactRunId(value)) return "spawn";
  return null;
}
