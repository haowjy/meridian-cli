import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { publisherFor, readBoundaryCapability, type BoundaryIdentity } from "../../shared/session_boundary";

export default function sessionBoundaryExtension(pi: ExtensionAPI): void {
  const capability = readBoundaryCapability();
  if (!capability) throw new Error("Pi session-boundary bundle loaded without a launch capability");
  const publisher = publisherFor(capability);
  publisher.initialize();
  registerSessionBoundaryHooks(pi, publisher);
}

export function registerSessionBoundaryHooks(pi: ExtensionAPI, publisher: ReturnType<typeof publisherFor>): void {
  // Each lifecycle hook catches only to poison publication state, then rethrows so
  // Pi's own extension_error protocol can veto qualification in the connection owner.
  pi.on("session_start", (event) => observeSafely(publisher, () => publisher.observe({ type: "session_start", reason: event.reason })));
  pi.on("session_before_switch", (event) => observeSafely(publisher, () => publisher.observe({ type: "session_before_switch", reason: event.reason })));
  pi.on("session_shutdown", (event, context) => {
    observeSafely(publisher, () => {
      const identity: BoundaryIdentity | undefined = event.reason === "quit"
        ? { session_id: context.sessionManager.getSessionId(), session_file: context.sessionManager.getSessionFile() }
        : undefined;
      publisher.observe({ type: "session_shutdown", reason: event.reason, identity });
    });
  });
}

function observeSafely(publisher: ReturnType<typeof publisherFor>, observe: () => void): void {
  try { observe(); } catch (error) {
    publisher.poison("observer_fault");
    throw error;
  }
}
