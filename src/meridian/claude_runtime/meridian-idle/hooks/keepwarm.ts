import type { MeridianIdleKeepwarm } from '../types/meridian-idle'

// Keepwarm: the pieces that need no host. Orchestration (timers, quiet window, turn matching) is in register.ts.

/** Pings per stretch when core does not send `keepwarm_pings`. Three keep a 1-hour cache warm for about 3.5 hours for less than one rebuild. */
export const DEFAULT_PINGS = 3

export type Keepwarm = MeridianIdleKeepwarm

export const FRESH: Keepwarm = { on: false, session: '', stretch: 0, sent: 0, limit: DEFAULT_PINGS, lead: 0, warned: false, reminder: false }

export function pingText(nonce: string): string {
  return (
    `[meridian-idle keepwarm ${nonce}]\n\n` +
    'Waiting for the user, keeping warm. To stop, reply with exactly stop-waiting and nothing else: ' +
    "that ends this stretch's pings, so you stop waiting for the user and accept the cache going cold before they return."
  )
}

/** The state reminder: the model writes what only the conversation knows into the work item, then ends its turn. Never mentions stop-waiting: it is not a ping. */
export function reminderText(nonce: string): string {
  return (
    `[meridian-idle state-reminder ${nonce}]\n\n` +
    'The user is away and your prompt cache is about to expire. Run `meridian work show` and update the ' +
    "work item's recorded state in place: status, decisions with their reasons, open questions, promises " +
    'to other sessions, where you stopped. If nothing changed, write nothing. End your turn with one line.'
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
