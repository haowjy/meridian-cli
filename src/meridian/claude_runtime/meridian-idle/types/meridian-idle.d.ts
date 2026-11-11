/**
 * Keepwarm's session-wide state, in `$.state` so a hot reload keeps it (a restart drops it: off is the safe default).
 * `sent` belongs to `session` and core's `stretch`; a different pair starts it at 0.
 */
export type MeridianIdleKeepwarm = {
  on: boolean
  session: string
  stretch: number
  /** Pings submitted this stretch. At the cap the stretch's pings are over (cap reached or the model stopped). */
  sent: number
  /** Milliseconds core placed between a turn's end and the warning; later pings come this long after each arm. 0 = unknown. */
  lead: number
}

declare module 'claude-code' {
  interface PluginState {
    'meridian-idle': { keepwarm: MeridianIdleKeepwarm }
  }
}
