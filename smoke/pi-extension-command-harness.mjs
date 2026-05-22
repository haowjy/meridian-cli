#!/usr/bin/env node
/**
 * Fake-Pi harness: load extensions, capture registerCommand notify output.
 * Usage: node smoke/pi-extension-command-harness.mjs [background-tasks|meridian-spawn-watch|both]
 */
import { pathToFileURL } from "node:url";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const extRoot = path.join(repoRoot, "src/meridian/pi_runtime/dist/extensions");

const mode = process.argv[2] ?? "both";
const notifications = [];

function makePi() {
  const commands = new Map();
  const tools = new Map();
  const listeners = new Map();
  const on = (event, handler) => {
    const bucket = listeners.get(event) ?? [];
    bucket.push(handler);
    listeners.set(event, bucket);
  };
  const emit = async (event, ...args) => {
    for (const handler of listeners.get(event) ?? []) {
      await handler(...args);
    }
  };
  return {
    registerCommand(name, spec) {
      commands.set(name, spec);
    },
    registerTool(reg) {
      const name = reg.name ?? reg.tool?.name;
      if (name) {
        tools.set(name, reg);
      }
    },
    on,
    events: { emit, on },
    session: { on, sendMessage: async () => {} },
    _commands: commands,
    _tools: tools,
    _emit: emit,
  };
}

async function loadExt(rel) {
  const url = pathToFileURL(path.join(extRoot, rel, "index.js")).href;
  return import(url);
}

async function runBackgroundTasks(pi) {
  const mod = await loadExt("background-tasks");
  await mod.default(pi);
  await pi._emit("session_start", {}, {
    cwd: repoRoot,
    sessionManager: { getSessionId: () => "smoke-session-bt" },
  });
  const ctx = {
    hasUI: true,
    ui: {
      notify(msg, level) {
        notifications.push({ source: "bt", cmd: "notify", level, msg: String(msg) });
      },
    },
    cwd: repoRoot,
    sessionManager: { getSessionId: () => "smoke-session-bt" },
  };
  await pi._commands.get("ps")?.handler("", ctx);
  const tool = pi._tools.get("background_task");
  if (!tool?.execute) {
    throw new Error("background_task tool missing");
  }
  const start = await tool.execute(
    "tc1",
    { action: "start", command: "sleep 2 && echo smoke-done", label: "smoke-task" },
    null,
    null,
    ctx,
  );
  notifications.push({ source: "bt", cmd: "tool:start", msg: JSON.stringify(start.details ?? {}) });
  await new Promise((r) => setTimeout(r, 2500));
  await pi._commands.get("ps")?.handler("", ctx);
  const taskId = start.details?.task_id ?? start.details?.task?.task_id;
  if (taskId) {
    await pi._commands.get("ps:logs")?.handler(String(taskId), ctx);
    await pi._commands.get("ps:clear")?.handler("", ctx);
  }
  await pi._commands.get("ps")?.handler("json", ctx);
}

async function runSpawnWatch(pi) {
  const mod = await loadExt("meridian-spawn-watch");
  mod.default(pi);
  await pi._emit("session_start", {}, {
    sessionManager: { getSessionId: () => "smoke-session-sw" },
  });
  const ctx = {
    hasUI: true,
    ui: {
      notify(msg, level) {
        notifications.push({ source: "sw", cmd: "notify", level, msg: String(msg) });
      },
    },
    sessionManager: { getSessionId: () => "smoke-session-sw" },
  };
  await pi._commands.get("spawns")?.handler("", ctx);
  await pi._commands.get("spawns:clear")?.handler("", ctx);
}

async function main() {
  process.env.MERIDIAN_PI_STATE_DIR =
    process.env.MERIDIAN_PI_STATE_DIR ?? `${process.env.HOME}/meridian-pi/smoke-pi-bg-tasks-20260522`;
  process.env.MERIDIAN_PI_SESSION_ROLE = "primary";

  if (mode === "background-tasks" || mode === "both") {
    const pi = makePi();
    await runBackgroundTasks(pi);
  }
  if (mode === "meridian-spawn-watch" || mode === "both") {
    const pi = makePi();
    await runSpawnWatch(pi);
  }

  process.stdout.write("@@SMOKE@@" + JSON.stringify({ ok: true, notifications }) + "\n");
}

main().catch((err) => {
  process.stdout.write(
    "@@SMOKE@@" + JSON.stringify({ ok: false, error: String(err?.message ?? err) }) + "\n",
  );
  process.exit(1);
});
