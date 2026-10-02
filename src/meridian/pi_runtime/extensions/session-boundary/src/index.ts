import type { ExtensionAPI, ExtensionContext, SessionStartEvent, SessionBeforeSwitchEvent, SessionShutdownEvent } from "@earendil-works/pi-coding-agent";
import { publisherFor, readBoundaryCapability, type BoundaryIdentity } from "../../shared/session_boundary";

export default function sessionBoundaryExtension(pi: ExtensionAPI): void {
  const capability = readBoundaryCapability();
  if (!capability) throw new Error("Pi boundary extension requires a launch capability");
  const publisher = publisherFor(capability);
  const observe = (event: SessionStartEvent | SessionBeforeSwitchEvent | SessionShutdownEvent, context: ExtensionContext): void => {
      const type = event.type;
      let identity: BoundaryIdentity | undefined;
      try {
        const sessionFile = context.sessionManager.getSessionFile();
        if (sessionFile === undefined) throw new Error("Pi native session has no file");
        identity = {
          session_id: context.sessionManager.getSessionId(),
          session_file: sessionFile,
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
  };
  pi.on("session_start", observe);
  pi.on("session_before_switch", observe);
  pi.on("session_shutdown", observe);
}
