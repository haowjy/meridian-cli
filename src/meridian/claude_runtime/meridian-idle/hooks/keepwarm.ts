import type { MeridianIdleKeepwarm } from '../types/meridian-idle'

// Keepwarm: the pieces that need no host. Orchestration (timers, quiet window, turn matching) is in register.ts.

/** Pings per stretch when core does not send `keepwarm_pings`. Three keep a 1-hour cache warm for about 3.5 hours for less than one rebuild. */
export const DEFAULT_PINGS = 3

export type Keepwarm = MeridianIdleKeepwarm

export const FRESH: Keepwarm = { on: false, session: '', stretch: 0, sent: 0, limit: DEFAULT_PINGS, lead: 0, warned: false, reminder: false }

const STOP = 'stop-waiting'
const COMPACT = 'compact-now'

export function pingText(nonce: string): string {
  return (
    `[meridian-idle keepwarm ${nonce}]\n\n` +
    'Waiting for the user, keeping warm. To stop, reply with exactly stop-waiting and nothing else: ' +
    "that ends this stretch's pings, so you stop waiting for the user and accept the cache going cold before they return."
  )
}

/**
 * The state reminder: the model writes what only the conversation knows into the work item, then ends its reply with its choice.
 * `stop-waiting` is offered only with keepwarm on: with it off there are no pings to stop.
 */
export function reminderText(nonce: string, keepwarmOn: boolean): string {
  return (
    `[meridian-idle state-reminder ${nonce}]\n\n` +
    'This is a keepwarm turn: the user is away and this turn keeps your prompt cache warm, which is about to expire. ' +
    'Run `meridian work show` and update the work item\'s recorded state in place: status, decisions with their reasons, ' +
    'open questions, promises to other sessions, where you stopped. If nothing changed, write nothing. ' +
    'Then end your reply with a last line that is exactly one of:\n' +
    `- ${COMPACT}: the work item now holds what you need to resume. Meridian compacts the conversation now, while the cache is warm, ` +
    'and the user returns to a small context. Choose it only after the update above.\n' +
    (keepwarmOn
      ? `- ${STOP}: ends this stretch's pings; you accept the cache going cold before the user returns.\n`
      : '') +
    '- anything else: keep waiting with the conversation as it is.'
  )
}

/** What the model is told to keep through a `compact-now` compaction: the work item already holds the rest. */
export const COMPACT_INSTRUCTIONS =
  'The work item (`meridian work show`) holds the current state of the work; do not repeat it. Keep what it does not: ' +
  "the user's latest requests in their own words, anything not yet recorded there, and what is needed to pick up " +
  'where the conversation stopped.'

export type Choice = typeof COMPACT | typeof STOP

const QUOTES: ReadonlyArray<readonly [string, string]> = [
  ["'", "'"],
  ['"', '"'],
  ['‘', '’'],
  ['“', '”'],
  ['`', '`'],
]

/** The reply's last non-empty line, alone or inside one pair of quotes or backticks, when it is a choice; `stop-waiting.` is not one. */
export function parseChoice(answer: string): Choice | undefined {
  const line = answer.trim().split('\n').at(-1)?.trim() ?? ''
  const pair = QUOTES.find(([open, close]) => line.length >= 2 && line.startsWith(open) && line.endsWith(close))
  const word = pair === undefined ? line : line.slice(pair[0].length, line.length - pair[1].length)
  return word === STOP || word === COMPACT ? word : undefined
}
