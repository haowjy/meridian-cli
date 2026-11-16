/**
 * Keepwarm's session-wide state, in `$.state` so a hot reload keeps it (a restart, a resume or `/clear` drops it, and the config default applies again).
 * `sent`, `warned` and `reminder` belong to `session` and core's `stretch`; a different pair starts them over.
 */
export type MeridianIdleKeepwarm = {
  on: boolean
  session: string
  stretch: number
  /** Pings submitted this stretch. At `limit` the stretch's pings are over (limit reached, or a stop wrote `sent = limit`). */
  sent: number
  /** The ping count the stretch started with, from config; a later config change does not reopen or close a stretch. */
  limit: number
  /** Milliseconds core placed between a turn's end and the warning; later pings come this long after each arm. 0 = unknown. */
  lead: number
  /** This stretch's warning acted (core did not skip it): only then do chained pings follow, so a skipped warning starts none. */
  warned: boolean
  /** The warning acted and core asked for a state reminder that is not yet submitted; whatever sends the stretch's next prompt sends it first. */
  reminder: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'meridian-idle': { keepwarm: MeridianIdleKeepwarm }
  }
}
