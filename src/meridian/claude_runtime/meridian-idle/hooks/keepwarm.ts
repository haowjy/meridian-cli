import type { MeridianIdleKeepwarm } from '../types/meridian-idle'

// Keepwarm: the pieces that need no host. Orchestration (timers, quiet window, turn matching) is in register.ts.

/** Pings per absence. Three keep a 1-hour cache warm for about 3.5 hours for less than one rebuild. */
export const PING_CAP = 3
/** The experiment log keeps this many records, as `LOG_LIMIT` caps `log`. */
export const RECORD_LIMIT = 200
const EXCERPT_CHARS = 200

export type Keepwarm = MeridianIdleKeepwarm

export const FRESH: Keepwarm = { on: false, session: '', stretch: 0, sent: 0, lead: 0, warned: false }

export type Outcome = 'stopped' | 'continued' | 'failed'

/** One submitted ping, as stored under the `keepwarm` key. */
export type PingRecord = {
  at: string
  session: string
  stretch: number
  ping: number
  outcome: Outcome
  /** Names of the main-loop tools denied during the ping's turn. */
  denied: string[]
  reply: string
  /** Why a ping `failed`. */
  why?: string
}

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

export function excerpt(answer: string): string {
  const flat = answer.trim().replace(/\s+/g, ' ')
  return flat.length > EXCERPT_CHARS ? `${flat.slice(0, EXCERPT_CHARS)}…` : flat
}

/** `records` plus `add`, newest last, capped. */
export function appended(records: unknown, add: PingRecord): PingRecord[] {
  const kept = Array.isArray(records) ? (records as PingRecord[]) : []
  return [...kept, add].slice(-RECORD_LIMIT)
}

/** The `/meridian-idle` lines for the experiment log: the tally, then the latest records. */
export function recordLines(records: unknown): string[] {
  if (!Array.isArray(records) || records.length === 0) return ['pings logged: none']
  const all = records as PingRecord[]
  const count = (outcome: Outcome) => all.filter(record => record.outcome === outcome).length
  const lines = [
    `pings logged: ${count('stopped')} stopped, ${count('continued')} continued, ${count('failed')} failed (last ${RECORD_LIMIT})`,
  ]
  for (const record of all.slice(-3)) {
    const denied = record.denied.length > 0 ? ` denied: ${record.denied.join(',')};` : ''
    lines.push(`  ${record.at} #${record.ping} ${record.outcome};${denied} ${record.why ?? record.reply}`.trimEnd())
  }
  return lines
}
