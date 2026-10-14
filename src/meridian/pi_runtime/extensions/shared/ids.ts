import { parseRef } from "./meridian_spawn";

export function isBashId(value: string | undefined | null): value is string {
  return typeof value === "string" && /^b-[0-9a-f]{8}$/i.test(value);
}

export function isSpawnId(value: string | undefined | null): value is string {
  return typeof value === "string" && parseRef(value).runId !== undefined;
}

export function classifyWorkId(value: string | undefined | null): "bash" | "spawn" | null {
  if (isBashId(value)) return "bash";
  if (isSpawnId(value)) return "spawn";
  return null;
}
