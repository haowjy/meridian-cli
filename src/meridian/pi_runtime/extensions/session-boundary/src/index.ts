import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { publisherFor, readBoundaryCapability, type BoundaryIdentity } from "../../shared/session_boundary";

type BoundaryContext = {
  sessionManager?: {
    getSessionId?: () => string | undefined;
    getSessionFile?: () => string | undefined;
  };
};

type BoundaryShutdown = { reason?: string };

export default function sessionBoundaryExtension(pi: ExtensionAPI): void {
  const capability = readBoundaryCapability();
  if (!capability) throw new Error("Pi session-boundary bundle loaded without a launch capability");
  const publisher = publisherFor(capability);
  publisher.initialize();
  registerSessionBoundaryHooks(pi, publisher);
}

export function registerSessionBoundaryHooks(pi: ExtensionAPI, publisher: ReturnType<typeof publisherFor>): void {
  const api = pi as ExtensionAPI & {
    on?: (event: string, handler: (...args: unknown[]) => unknown) => void;
  };

  // Each lifecycle hook catches only to poison publication state, then rethrows so
  // Pi's own extension_error protocol can veto qualification in the connection owner.
  api.on?.("session_start", () => observeSafely(publisher, () => publisher.observe({ type: "session_start" })));
  api.on?.("session_before_switch", () => observeSafely(publisher, () => publisher.observe({ type: "session_before_switch" })));
  api.on?.("session_shutdown", (event, context) => {
    observeSafely(publisher, () => {
      const shutdown = event as BoundaryShutdown;
      const ctx = context as BoundaryContext;
      const identity: BoundaryIdentity | undefined = shutdown.reason === "quit"
        ? { session_id: ctx.sessionManager?.getSessionId?.() ?? "", session_file: ctx.sessionManager?.getSessionFile?.() ?? "" }
        : undefined;
      publisher.observe({ type: "session_shutdown", reason: shutdown.reason ?? "", identity });
    });
  });
}

function observeSafely(publisher: ReturnType<typeof publisherFor>, observe: () => void): void {
  try { observe(); } catch (error) {
    publisher.poison("observer_fault");
    throw error;
  }
}
