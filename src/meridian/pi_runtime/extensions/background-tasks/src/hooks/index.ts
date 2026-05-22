import type { TaskRegistry } from "../task_registry";

type PiWithHooks = {
  registerHook?: (name: string, handler: (...args: unknown[]) => unknown) => void;
  on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
};

/** Optional status-line hook when Pi exposes registerHook. */
export function setupBackgroundTaskHooks(
  pi: PiWithHooks,
  state: { registry: TaskRegistry | null },
): void {
  pi.on?.("agent_end", async () => {
    const registry = state.registry;
    if (!registry) {
      return;
    }
    const running = (await registry.list(false)).length;
    if (running > 0) {
      // no-op; future: ctx.ui.setStatus(`tasks: ${running} running · /ps`)
    }
  });
}
