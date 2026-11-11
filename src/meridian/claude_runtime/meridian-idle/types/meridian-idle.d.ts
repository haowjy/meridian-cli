/**
 * Keepwarm's session-wide state, in `$.state` so a hot reload keeps it (a restart drops it: off is the safe default).
 * `sent` and `warned` belong to `session` and core's `stretch`; a different pair starts them over.
 */
export type MeridianIdleKeepwarm = {
  on: boolean
  session: string
  stretch: number
  /** Pings submitted this stretch. At the cap the stretch's pings are over (cap reached or the model stopped). */
  sent: number
  /** Milliseconds core placed between a turn's end and the warning; later pings come this long after each arm. 0 = unknown. */
  lead: number
  /** This stretch's warning acted (core did not skip it): only then do chained pings follow, so a skipped warning starts none. */
  warned: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'meridian-idle': { keepwarm: MeridianIdleKeepwarm }
  }
}
