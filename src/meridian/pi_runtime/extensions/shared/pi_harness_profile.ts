/**
 * TS mirror of Python `PiExtensionLaunchProfile` / `[harness.pi]` bundle toggles.
 * Python omits disabled bundles from `-e`; extensions no-op when loaded anyway.
 */

export type PiExtensionLaunchProfile = {
  background_tasks_enabled: boolean;
  spawn_watch_enabled: boolean;
  interactive: boolean;
};

function truthyEnv(raw: string | undefined): boolean {
  if (raw == null) {
    return false;
  }
  const normalized = raw.trim().toLowerCase();
  return normalized.length > 0 && !["0", "false", "no", "off"].includes(normalized);
}

function envFlag(name: string): boolean | null {
  const raw = process.env[name];
  if (raw == null) {
    return null;
  }
  return truthyEnv(raw);
}

/** Resolve bundle toggles from env (matches `resolve_pi_harness_profile()` overrides). */
export function resolvePiHarnessProfile(): {
  background_tasks_enabled: boolean;
  spawn_watch_enabled: boolean;
  disable_managed_bash: boolean;
} {
  let background_tasks_enabled = true;
  let spawn_watch_enabled = true;

  const disableManaged =
    envFlag("MERIDIAN_PI_DISABLE_MANAGED_BASH") === true ||
    process.env.MERIDIAN_PI_MANAGED_BASH?.trim() === "0";
  if (disableManaged) {
    background_tasks_enabled = false;
  }

  const bg = envFlag("MERIDIAN_PI_BACKGROUND_TASKS_ENABLED");
  if (bg != null) {
    background_tasks_enabled = bg;
  }

  const sw = envFlag("MERIDIAN_PI_SPAWN_WATCH_ENABLED");
  if (sw != null) {
    spawn_watch_enabled = sw;
  }

  return {
    background_tasks_enabled,
    spawn_watch_enabled,
    disable_managed_bash: disableManaged,
  };
}

export function resolvePiExtensionLaunchProfile(
  interactive: boolean,
): PiExtensionLaunchProfile {
  const profile = resolvePiHarnessProfile();
  return {
    background_tasks_enabled: profile.background_tasks_enabled,
    spawn_watch_enabled: profile.spawn_watch_enabled,
    interactive,
  };
}

export function isBackgroundTasksExtensionEnabled(interactive = true): boolean {
  const launch = resolvePiExtensionLaunchProfile(interactive);
  return launch.interactive && launch.background_tasks_enabled;
}

export function isSpawnWatchExtensionEnabled(): boolean {
  return resolvePiHarnessProfile().spawn_watch_enabled;
}
