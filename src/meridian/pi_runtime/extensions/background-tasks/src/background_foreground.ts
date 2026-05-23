import { getForegroundUserBashTaskId } from "./bash_bridge";
import type { TaskPanelHost } from "./panel/host";

/** Background the interactive `$` task blocking the foreground slot (same path as `/ps` + `b`). */
export async function backgroundForegroundBash(
  host: TaskPanelHost,
): Promise<{ ok: boolean; reason?: string }> {
  const id = getForegroundUserBashTaskId();
  if (!id) {
    return { ok: false, reason: "no_foreground" };
  }
  return host.backgroundTask(id);
}
