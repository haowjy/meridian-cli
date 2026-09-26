import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { publisherFor, readBoundaryCapability, type BoundaryIdentity } from "../../shared/session_boundary";

export default function sessionBoundaryExtension(pi: ExtensionAPI): void {
  const capability = readBoundaryCapability();
  if (!capability) throw new Error("Pi boundary extension requires a launch capability");
  const publisher = publisherFor(capability);
  for (const type of ["session_start", "session_before_switch", "session_shutdown"] as const) {
    pi.on(type, (event, context) => {
      let identity: BoundaryIdentity | undefined;
      try {
        identity = {
          session_id: context.sessionManager.getSessionId(),
          session_file: context.sessionManager.getSessionFile(),
        };
      } catch (error) {
        // Pi 0.87.1 can race RPC EOF against replacement and call even a
        // fresh shutdown ctx on an invalidated runner. This is not evidence
        // of an identity conflict; publish the shutdown without an identity.
        if (type !== "session_shutdown" || !(error instanceof Error) ||
            !error.message.startsWith("This extension ctx is stale after session replacement or reload.")) {
          publisher.poison("identity_read_fault");
          throw error;
        }
      }
      publisher.observe({ type, reason: event.reason, identity });
    });
  }
}
