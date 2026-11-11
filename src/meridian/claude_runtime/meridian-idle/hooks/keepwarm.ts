import type { MeridianIdleKeepwarm } from '../types/meridian-idle'

// Keepwarm: the pieces that need no host. Orchestration (timers, quiet window, turn matching) is in register.ts.

/** Pings per absence. Three keep a 1-hour cache warm for about 3.5 hours for less than one rebuild. */
export const PING_CAP = 3

export type Keepwarm = MeridianIdleKeepwarm

export const FRESH: Keepwarm = { on: false, session: '', stretch: 0, sent: 0, lead: 0, warned: false }

export function pingText(nonce: string): string {
  return (
    `[meridian-idle keepwarm ${nonce}]\n\n` +
    'Waiting for the user, keeping warm. To stop waiting for the user, reply with exactly stop-waiting and nothing else.'
  )
}

const STOP = 'stop-waiting'
const QUOTES: ReadonlyArray<readonly [string, string]> = [
  ["'", "'"],
  ['"', '"'],
  ['‘', '’'],
  ['“', '”'],
  ['`', '`'],
]

/** The whole reply is `stop-waiting`, alone or inside one pair of quotes or backticks. `stop-waiting.` is not a stop. */
export function isStop(answer: string): boolean {
  const text = answer.trim()
  return text === STOP || QUOTES.some(([open, close]) => text === `${open}${STOP}${close}`)
}
