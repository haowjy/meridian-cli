import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { TaskPanelHost } from "../panel/host";
import type { DockActions } from "../hooks/widget";
import type { TaskRegistry } from "../task_registry";
import type { BackgroundTaskRecord, PsRow } from "../types";
import { registerPsBackgroundCommands } from "./background";
import { registerPsClearCommand } from "./clear";
import { registerPsDockCommand } from "./dock";
import { registerPsKillCommand } from "./kill";
import { registerPsLogsCommand } from "./logs";
import { registerPsPinCommand } from "./pin";
import { registerPsCommand } from "./ps";
import { registerPsSettingsCommand } from "./settings";

export function setupPsCommands(
  pi: ExtensionAPI,
  panelHost: TaskPanelHost,
  dockActions: DockActions,
  host: {
    getRegistry: () => TaskRegistry | null;
    mergeRows: (tasks: BackgroundTaskRecord[]) => PsRow[];
  },
): void {
  registerPsCommand(pi, panelHost, dockActions, host);
  registerPsBackgroundCommands(pi, panelHost);
  registerPsLogsCommand(pi, panelHost);
  registerPsPinCommand(pi, panelHost, dockActions);
  registerPsKillCommand(pi, panelHost, dockActions, host);
  registerPsClearCommand(pi, panelHost);
  registerPsDockCommand(pi, dockActions);
  registerPsSettingsCommand(pi);
}
