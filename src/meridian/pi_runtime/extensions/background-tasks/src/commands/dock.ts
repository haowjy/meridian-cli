import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { DockActions } from "../hooks/widget";

export function registerPsDockCommand(pi: ExtensionAPI, dockActions: DockActions): void {
  pi.registerCommand("ps:dock", {
    description: "Control log dock visibility (show | hide | toggle)",
    handler: async (args) => {
      const arg = args.trim().toLowerCase();
      if (arg === "show") {
        dockActions.expand();
      } else if (arg === "hide") {
        dockActions.hide();
      } else {
        dockActions.toggle();
      }
    },
  });
}
