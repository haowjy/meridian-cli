import path from "node:path";

/** Mirrors ``pi_paths.resolve_meridian_pi_state_dir`` — extension runtime state root. */
export function resolveStateRoot(): string {
  const explicit = process.env.MERIDIAN_PI_STATE_DIR?.trim();
  if (explicit) {
    return explicit;
  }
  const sessionDir = process.env.PI_CODING_AGENT_SESSION_DIR?.trim();
  if (sessionDir) {
    return sessionDir;
  }
  const agentDir = process.env.PI_CODING_AGENT_DIR?.trim();
  if (agentDir) {
    return path.join(agentDir, ".meridian");
  }
  const home = process.env.HOME?.trim();
  if (home) {
    return path.join(home, ".meridian", "meridian-pi", "state");
  }
  return path.join(process.cwd(), ".meridian");
}
