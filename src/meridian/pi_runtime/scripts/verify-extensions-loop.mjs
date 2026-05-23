#!/usr/bin/env node
/** Rebuild Pi extensions and run vitest on an interval (local CI loop). */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const intervalMs = Number.parseInt(process.env.VERIFY_INTERVAL_MS ?? "60000", 10);

function run(label, args) {
  const result = spawnSync("npm", args, {
    cwd: root,
    stdio: "inherit",
    env: process.env,
  });
  if (result.status !== 0) {
    console.error(`[verify-loop] ${label} failed (exit ${result.status ?? 1})`);
    return false;
  }
  return true;
}

console.log(`[verify-loop] watching every ${intervalMs}ms — Ctrl+C to stop`);

for (;;) {
  const started = new Date().toISOString();
  console.log(`\n[verify-loop] === ${started} ===`);
  const ok = run("verify:extensions", ["run", "verify:extensions"]);
  if (!ok) {
    process.exitCode = 1;
  }
  await new Promise((resolve) => setTimeout(resolve, intervalMs));
}
