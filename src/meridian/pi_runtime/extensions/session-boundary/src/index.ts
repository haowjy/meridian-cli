import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { publisherFor, readBoundaryCapability } from "../../shared/session_boundary";

export default function sessionBoundaryExtension(pi: ExtensionAPI): void {
  const capability = readBoundaryCapability();
  if (!capability) throw new Error("Pi boundary extension requires a launch capability");
  const publisher = publisherFor(capability);
  for (const type of ["session_start", "session_before_switch", "session_shutdown"] as const) {
    pi.on(type, (event, context) => {
      try {
        publisher.observe({
          type, reason: event.reason,
          ...(type === "session_before_switch" ? {} : { identity: {
            session_id: context.sessionManager.getSessionId(),
            session_file: context.sessionManager.getSessionFile(),
          } }),
        });
      } catch (error) {
        publisher.poison("observer_fault");
        throw error;
      }
    });
  }
}
