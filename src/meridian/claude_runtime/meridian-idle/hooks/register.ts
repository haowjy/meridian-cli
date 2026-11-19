import type { ClassicHookInputs, EngineInterface, Register, Timer } from 'claude-code'

import {
  COMPACT_INSTRUCTIONS,
  DEFAULT_PINGS,
  FRESH,
  parseChoice,
  pingText,
  reminderText,
  type Keepwarm,
} from './keepwarm'

// Meridian's Claude idle sensor.
//
// This module senses three things and hands every decision to `meridian idle`:
//   idle    a main-loop turn finished        -> `idle arm` with the work still running, then one timer per stage
//   return  the user typed or sent something -> `idle return`, timers cancelled
//   permission  Claude still awaits approval -> `idle needs-permission`, with no state change
//   keepwarm  the cache warning fired and the user opted in -> up to `keepwarm_pings` prompts that keep the cache warm (see keepwarm.ts)
//   reminder  the cache warning fired and core said `remind` -> one prompt asking the model to record its state; it takes ping 1's place
// Notifications belong to core; this file never sends one, reads
// meridian config or env, or writes under `~/.meridian`. Nothing here writes to the terminal: diagnostics go to the
// plugin's own `$.store` key `log`, and `/meridian-idle` prints them on request. `/keepwarm` toggles or sets keepwarm.

type Stage = 'push' | 'warn'

const STAGES: readonly Stage[] = ['push', 'warn']
const HARNESS = 'claude'
const COMMAND = 'meridian-idle'
const KEEPWARM_COMMAND = 'keepwarm'
const CLI_TIMEOUT_MS = 20_000
const LOG_LIMIT = 100
const EXCERPT_CHAR_CAP = 4_000
// One JSON argv entry stays below ~50 KiB even if every command code unit needs
// JSON's longest escape, well clear of Linux's 128 KiB per-argument limit.
const BACKGROUND_TASK_LIMIT = 16
const BACKGROUND_COMMAND_CHAR_CAP = 512
/** The prompt box must be empty and untouched this long before a ping starts a turn under the user. */
const QUIET_MS = 10_000
const HOLD_POLL_MS = 1_000
/** Why a ping waited past `deadline`: ends the stretch's pings. */
const COLD = 'the cache would be cold'
/** A ping gives up this long before the cache expires: a ping sent then would rebuild a cold cache. */
const EXPIRY_MARGIN_MS = 60_000
// The host lists the state a module touches, so the reference must be literal in this file.
const IDLE_KEEPWARM = { plugin: 'meridian-idle', key: 'keepwarm' } as const

type Json = Record<string, unknown>
type BackgroundTask = NonNullable<ClassicHookInputs['Stop']['background_tasks']>[number]

/** One open stretch as the mod knows it: the ids `fire` needs, and the absolute stage times. */
type Armed = {
  session: string
  stretch: number
  anchor: number
  at: Partial<Record<Stage, number>>
  /** What core answered when each stage's timer fired, for `/meridian-idle`. */
  fired: Partial<Record<Stage, string>>
}

type PromptKind = 'ping' | 'reminder'

/** The ping that was submitted and has not completed. Only one exists at a time. */
type Ping = {
  nonce: string
  /** A reminder is a prompt too, but asks for writes and has no stop reply. */
  kind: PromptKind
  /** The keepwarm slot taken, or 0 for a reminder sent without one (keepwarm off or its pings used up). */
  index: number
  /** `stop-waiting` was offered, and so counts: always for a ping, for a reminder only with keepwarm on. Set once, beside `index`. */
  offersStop: boolean
  session: string
  stretch: number
  /** `submitted`: no main-loop turn carrying the nonce has started yet. `running`: that turn is in flight (`turnId`). */
  phase: 'submitted' | 'running'
  turnId: string | undefined
  /** The user typed during the ping turn: tools are theirs to steer again. */
  userCame: boolean
}

type Policy = {
  /** What `/keepwarm` has not yet overridden this session. */
  keepwarm: boolean
  /** `warn_minutes` in ms: the time left on the cache when the warning fires. */
  warnMs: number | undefined
  /** Pings per stretch; a stretch's record keeps the limit it started with. */
  pings: number
}

type State = {
  /** Print mode or no surface: the whole session is ignored. */
  inert: boolean
  /** Memoised `idle config --interactive` verdict. */
  gate: Promise<boolean> | undefined
  /** The `idle config` reply, set once by `checkGate`; no other field holds a config-derived value. */
  policy: Policy
  armed: Armed | undefined
  timers: Timer[]
  /** The chained ping's timer (pings 2 and on), kept apart so a compaction can cancel it alone. */
  pingTimer: Timer | undefined
  /** A main-conversation compaction is running: no ping may start, since it would prefill a conversation about to be replaced. */
  compacting: boolean
  /** Bumped by every user return. Async work compares it to detect that the user came back meanwhile. */
  epoch: number
  /** An `arm` reached core since the last `return`, so a `return` call has something to close. */
  mayBeOpen: boolean
  /** Serialises the short `meridian idle` calls so arm/return/fire reach core in hook order. */
  queue: Promise<unknown>
  log: string[]
  /** Last prompt typed by the human; tool results and injected hook turns never enter here. */
  lastUserText: string | undefined
  /** The main loop's in-flight background work, from the `Stop` that precedes its `turn.complete`. */
  stopTasks: BackgroundTask[] | undefined
  ping: Ping | undefined
  /** A main-loop turn is in flight (from its `turn.start` to its `turn.complete`). */
  running: boolean
  /** The last main-loop answer that was not a ping's: a ping turn's arm sends it, so the your-turn notice never reads the ping's reply. */
  lastAnswer: string | undefined
  /** When the prompt box was last touched, as `$.clock.now()` answered it; kept as the pending answer so hooks never wait on it. */
  lastChange: Promise<number>
  /** When the last main-loop turn ended, kept like `lastChange`: a compaction waits `QUIET_MS` after any turn, however short. */
  lastTurnEnd: Promise<number>
}

function newState(): State {
  return {
    inert: false,
    gate: undefined,
    policy: { keepwarm: false, warnMs: undefined, pings: DEFAULT_PINGS },
    armed: undefined,
    timers: [],
    pingTimer: undefined,
    compacting: false,
    epoch: 0,
    mayBeOpen: false,
    queue: Promise.resolve(),
    log: [],
    lastUserText: undefined,
    stopTasks: undefined,
    ping: undefined,
    running: false,
    lastAnswer: undefined,
    lastChange: Promise.resolve(Number.NEGATIVE_INFINITY),
    lastTurnEnd: Promise.resolve(Number.NEGATIVE_INFINITY),
  }
}

// -- small helpers ---------------------------------------------------------

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function capExcerpt(value: string | undefined): string | undefined {
  return value?.slice(0, EXCERPT_CHAR_CAP)
}

/** A prompt or command the user themselves sent. Injected origins (task-notification, peer, plugin, sdk, ...) are not returns. */
function isUserOrigin(origin: { kind: string } | undefined): boolean {
  return origin?.kind === 'composer' || origin?.kind === 'bridge'
}

function note($: EngineInterface, s: State, line: string): void {
  s.log.push(`${new Date().toISOString()} ${line}`)
  if (s.log.length > LOG_LIMIT) s.log.splice(0, s.log.length - LOG_LIMIT)
  $.store.set('log', s.log).catch(() => undefined)
}

function cancelTimers(s: State): void {
  for (const timer of s.timers) timer.cancel()
  s.timers = []
  s.pingTimer?.cancel()
  s.pingTimer = undefined
}

function enqueue(s: State, task: () => Promise<unknown>): Promise<unknown> {
  const run = s.queue.then(task).catch(() => undefined)
  s.queue = run
  return run
}

/** Runs `meridian idle <args>` in the session's environment and returns the parsed stdout (a JSON object or array). Never throws. */
async function cli($: EngineInterface, s: State, args: string[]): Promise<unknown> {
  try {
    const out = await $.process.run(['meridian', 'idle', ...args, '--interactive'], { timeoutMs: CLI_TIMEOUT_MS })
    const text = out.stdout.trim()
    if (text === '') return { error: `no output (exit ${out.exitCode}): ${out.stderr.trim().slice(0, 200)}` }
    try {
      return JSON.parse(text)
    } catch {
      return { error: `unparseable output (exit ${out.exitCode}): ${text.slice(0, 200)}` }
    }
  } catch (err) {
    return { error: describeError(err) }
  }
}

/** The error text of a `cli` result, or undefined when it succeeded. */
function failure(reply: unknown): string | undefined {
  if (!isRecord(reply)) return Array.isArray(reply) ? undefined : 'unexpected reply'
  return typeof reply.error === 'string' ? reply.error : undefined
}

function who(armed: Armed): string[] {
  return ['--harness', HARNESS, '--session', armed.session, '--stretch', String(armed.stretch)]
}

// -- gate: is this a primary TUI session with idle enabled? ----------------

async function checkGate($: EngineInterface, s: State): Promise<boolean> {
  const reply = await cli($, s, ['config', '--harness', HARNESS])
  const problem = failure(reply)
  if (problem !== undefined || !isRecord(reply)) {
    // Not memoised: a transient failure (meridian not on PATH yet) should not disable idle for the whole session.
    note($, s, `config failed: ${problem ?? 'unexpected reply'}`)
    s.gate = undefined
    return false
  }
  if (reply.enabled !== true) {
    s.inert = true
    note($, s, `inert: idle disabled (${typeof reply.reason === 'string' ? reply.reason : 'no reason given'})`)
    return false
  }
  const warnMinutes = asNumber(reply.warn_minutes)
  s.policy = {
    keepwarm: reply.keepwarm === true,
    warnMs: warnMinutes === undefined ? undefined : warnMinutes * 60_000,
    pings: asNumber(reply.keepwarm_pings) ?? DEFAULT_PINGS,
  }
  note($, s, 'active: idle enabled')
  return true
}

function gate($: EngineInterface, s: State): Promise<boolean> {
  if (s.inert) return Promise.resolve(false)
  s.gate ??= checkGate($, s)
  return s.gate
}

// -- arm / adopt timers ----------------------------------------------------

function readSchedule(row: unknown): Armed['at'] {
  const at: Armed['at'] = {}
  if (!isRecord(row)) return at
  for (const stage of STAGES) {
    const when = asNumber(row[`${stage}_at`])
    if (when !== undefined) at[stage] = when
  }
  return at
}

/** Replaces the pending timers with one per stage in `armed.at`, at its absolute epoch-ms time. */
async function adopt($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<void> {
  const now = await $.clock.now()
  if (s.epoch !== epoch) return // the user came back while we were waiting
  cancelTimers(s)
  s.armed = armed
  for (const stage of STAGES) {
    const when = armed.at[stage]
    if (when === undefined) continue
    s.timers.push(
      $.clock.after(Math.max(0, when - now), () => {
        void onTimer($, s, stage, armed, epoch)
      }),
    )
  }
  const plan = STAGES.filter(stage => armed.at[stage] !== undefined)
    .map(stage => `${stage}+${Math.round(((armed.at[stage] ?? now) - now) / 1000)}s`)
    .join(' ')
  note($, s, `armed stretch ${armed.stretch} anchor ${armed.anchor}: ${plan || 'no stages left'}`)
}

/** Rebuilds timers from the stored schedule of this session's open stretch (reload or resume), never from memory. */
async function recover($: EngineInterface, s: State, session: string, epoch: number): Promise<void> {
  const rows = await cli($, s, ['status', '--json'])
  if (!Array.isArray(rows)) {
    const problem = failure(rows)
    if (problem !== undefined) note($, s, `status failed: ${problem}`)
    return
  }
  const row = rows.find(
    candidate =>
      isRecord(candidate) &&
      candidate.harness === HARNESS &&
      candidate.session === session &&
      candidate.stretch_open === true,
  )
  if (!isRecord(row)) return
  const stretch = asNumber(row.stretch)
  const anchor = asNumber(row.anchor)
  if (stretch === undefined || anchor === undefined) return
  const done = isRecord(row.done) ? row.done : {}
  const at = readSchedule(row.schedule)
  for (const stage of STAGES) {
    if (done[stage] !== undefined) delete at[stage] // the stage already ran in this stretch
  }
  s.mayBeOpen = true
  const armed: Armed = { session, stretch, anchor, at, fired: {} }
  await adopt($, s, armed, epoch)
  await schedulePing($, s, armed, epoch) // a reload with a reminder owed (or pings left) resumes the stretch's prompts
}

/** Core's name for each kind of host background task. Core decides what each means for the push and the warning. */
const TASK_KINDS: Record<string, string> = { subagent: 'agent', workflow: 'agent', shell: 'shell', monitor: 'watcher' }

/** The work the main loop left running, as `idle arm --background-tasks` JSON. */
function backgroundTasks(tasks: BackgroundTask[]): string {
  return JSON.stringify(
    tasks.slice(0, BACKGROUND_TASK_LIMIT).map(task => {
      const shellCommand = task.type === 'shell' ? task.command : undefined
      const overCap = shellCommand !== undefined && shellCommand.length > BACKGROUND_COMMAND_CHAR_CAP
      return {
        kind: overCap ? 'other' : (TASK_KINDS[task.type] ?? 'other'),
        ...(shellCommand !== undefined && !overCap ? { command: shellCommand } : {}),
      }
    }),
  )
}

async function armTask(
  $: EngineInterface,
  s: State,
  epoch: number,
  assistantText: string | undefined,
  tasks: BackgroundTask[] | undefined,
): Promise<void> {
  if (!(await gate($, s))) return
  try {
    // Work still running does not stop the arm: core reads it and holds or delays the push.
    const session = await $.session.id()
    const args = ['arm', '--harness', HARNESS, '--session', session]
    if (tasks !== undefined && tasks.length > 0) args.push(`--background-tasks=${backgroundTasks(tasks)}`)
    const userExcerpt = capExcerpt(s.lastUserText)
    const assistantExcerpt = capExcerpt(assistantText)
    if (userExcerpt !== undefined) args.push(`--user-text=${userExcerpt}`)
    if (assistantExcerpt !== undefined) args.push(`--assistant-text=${assistantExcerpt}`)
    const reply = await cli($, s, args)
    const problem = failure(reply)
    if (problem !== undefined || !isRecord(reply)) {
      note($, s, `arm failed: ${problem ?? 'unexpected reply'}`)
      return
    }
    const stretch = asNumber(reply.stretch)
    const anchor = asNumber(reply.anchor)
    if (stretch === undefined || anchor === undefined) {
      // Core declined (role or idle gate): nothing is open, nothing to time.
      cancelTimers(s)
      s.armed = undefined
      note($, s, `arm declined${typeof reply.reason === 'string' ? ` (${reply.reason})` : ''}`)
      return
    }
    s.mayBeOpen = true
    if (s.epoch !== epoch) return // a return queued behind this arm closes the stretch; arm no timers for it
    const current = s.armed
    if (current !== undefined && current.session === session && current.stretch === stretch && current.anchor === anchor) {
      return // same stretch and anchor: core absorbed this arm (a duplicate turn); keep our timers
    }
    const warnAt = asNumber(reply.warn_at)
    await trackStretch($, s, session, stretch, warnAt === undefined ? undefined : warnAt - (await $.clock.now()))
    if (typeof reply.push_held === 'string') note($, s, `push held: ${reply.push_held}`)
    const at = readSchedule(reply)
    if (Object.keys(at).length === 0 && typeof reply.reason === 'string') {
      // An absorbed arm for a stretch we do not know yet carries no deadlines: take them from the store.
      await recover($, s, session, epoch)
      return
    }
    const armed: Armed = { session, stretch, anchor, at, fired: {} }
    await adopt($, s, armed, epoch)
    await schedulePing($, s, armed, epoch)
  } catch (err) {
    note($, s, `arm error: ${describeError(err)}`)
  }
}

// -- keepwarm ---------------------------------------------------------------

/** The session's record, else core's configured default: `/keepwarm` writes the record, and `/clear` drops it. */
async function readKeepwarm($: EngineInterface, s: State): Promise<Keepwarm> {
  return (await $.state.get(IDLE_KEEPWARM)).value ?? { ...FRESH, on: s.policy.keepwarm, limit: s.policy.pings }
}

async function keepwarmLine($: EngineInterface, s: State): Promise<string> {
  const kw = await readKeepwarm($, s)
  const armed = s.armed
  const sent = armed !== undefined && kw.session === armed.session && kw.stretch === armed.stretch ? kw.sent : 0
  return `keepwarm: ${kw.on ? 'on' : 'off'}, ${sent}/${kw.limit} pings this stretch`
}

/** The record as it stands for this stretch: a different session or core stretch starts the count, limit and lead over. */
async function stretchRecord($: EngineInterface, s: State, session: string, stretch: number): Promise<Keepwarm> {
  const kw = await readKeepwarm($, s)
  if (kw.session !== session || kw.stretch !== stretch) {
    return { ...kw, session, stretch, sent: 0, limit: s.policy.pings, lead: 0, warned: false, reminder: false }
  }
  // A record written by an older build has no limit: give it this session's.
  return asNumber(kw.limit) === undefined ? { ...kw, limit: s.policy.pings } : kw
}

/** The one place a stretch's pings end: the stop marker is `sent = limit`, so a changed config cannot reopen it. */
async function endPings($: EngineInterface, s: State, session: string, stretch: number, why: string): Promise<void> {
  const kw = await stretchRecord($, s, session, stretch)
  await $.state.set(IDLE_KEEPWARM, { ...kw, sent: kw.limit, reminder: false })
  note($, s, `this stretch's pings are over: ${why}`)
}

/** An arm reached core: record which stretch it belongs to and, when it placed the warning, the lead. */
async function trackStretch($: EngineInterface, s: State, session: string, stretch: number, lead: number | undefined): Promise<void> {
  const before = await readKeepwarm($, s)
  const kw = await stretchRecord($, s, session, stretch)
  const next = lead !== undefined && lead > 0 ? { ...kw, lead } : kw
  if (next.session !== before.session || next.stretch !== before.stretch || next.lead !== before.lead) {
    await $.state.set(IDLE_KEEPWARM, next)
  }
}

/**
 * What the stretch's next prompt is, or undefined when none is due. An owed reminder goes first and needs only that the stretch's
 * pings are not ended (`sent = limit`: a compaction, a cold cache or a stop); a ping needs keepwarm on and a slot left.
 */
function nextKind(kw: Keepwarm): PromptKind | undefined {
  if (kw.sent >= kw.limit) return undefined
  return kw.reminder ? 'reminder' : kw.on ? 'ping' : undefined
}

/** A running main-loop turn refreshes the cache and, when it ends, arms the next ping itself; a prompt now would queue behind it. */
function skipForRunningTurn($: EngineInterface, s: State, kind: PromptKind): boolean {
  if (!s.running) return false
  note($, s, `${kind} skipped: a turn is running; its arm schedules the next prompt`)
  return true
}

/** Resolves undefined once the prompt box is empty and untouched for `QUIET_MS`; otherwise why the ping must not go: the user returned, the stretch's pings ended or `deadline` (epoch ms) came first. `deadline` is the one cold-cache check: `runPing` ends the stretch's pings when it wins. A running compaction pauses the wait. */
async function holdForUser($: EngineInterface, s: State, armed: Armed, epoch: number, deadline: number, kind: PromptKind): Promise<string | undefined> {
  const RETURNED = 'the user returned'
  for (;;) {
    if (s.epoch !== epoch) return RETURNED
    if (nextKind(await stretchRecord($, s, armed.session, armed.stretch)) !== kind) return "nothing is due this stretch: its pings are over or keepwarm is off"
    const now = await $.clock.now()
    if (now >= deadline) return COLD
    // A landed compaction ends the wait through the cap; a skipped one lets the ping go.
    if (!s.compacting) {
      const changedAt = await s.lastChange
      const { text } = await $.prompt.read()
      if (s.epoch !== epoch) return RETURNED
      if (!s.compacting && text === '' && now - changedAt >= QUIET_MS) return undefined
    }
    await $.clock.sleep(HOLD_POLL_MS)
  }
}

function newNonce(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

/**
 * Takes the next ping slot of this stretch; undefined when the prompt must not go (a compaction, the user returned,
 * `deadline` passed, or `kind` is no longer what the stretch owes). A reminder takes ping 1's slot when keepwarm is on, else none (0) and clears the owed flag.
 */
async function reserveSlot($: EngineInterface, s: State, armed: Armed, epoch: number, deadline: number, kind: PromptKind): Promise<number | undefined> {
  const kw = await stretchRecord($, s, armed.session, armed.stretch)
  if (s.compacting || s.epoch !== epoch || nextKind(kw) !== kind || (await $.clock.now()) >= deadline) return undefined
  const slot = kw.on ? kw.sent + 1 : 0
  await $.state.set(IDLE_KEEPWARM, { ...kw, sent: kw.on ? slot : kw.sent, reminder: false })
  return slot
}

/**
 * Sends the stretch's next prompt (`nextKind`: an owed state reminder, else a keep-warm ping), once the box has been quiet. `dueAt` is when the stage's timer
 * was scheduled to fire: `warn_minutes` of cache remain then, so the ping gives up at `dueAt + warn_minutes - margin`
 * however late the timer ran. The count is taken before the submit so that a reload or a failure can only cost a
 * ping, never add one.
 */
async function runPing($: EngineInterface, s: State, armed: Armed, epoch: number, dueAt: number): Promise<void> {
  let kind: PromptKind = 'ping'
  try {
    const due = nextKind(await stretchRecord($, s, armed.session, armed.stretch))
    if (due === undefined) return
    kind = due
    if (s.policy.warnMs === undefined || s.policy.warnMs <= EXPIRY_MARGIN_MS) {
      note($, s, `${kind} skipped: no usable warn_minutes from core`)
      return
    }
    const deadline = dueAt + s.policy.warnMs - EXPIRY_MARGIN_MS
    if (skipForRunningTurn($, s, kind)) return
    const held = await holdForUser($, s, armed, epoch, deadline, kind)
    if (held === COLD) {
      await enqueue(s, () => endPings($, s, armed.session, armed.stretch, COLD))
      return
    }
    if (held !== undefined) {
      note($, s, `${kind} skipped: ${held}`)
      return
    }
    if (skipForRunningTurn($, s, kind)) return // a turn started while the prompt waited
    const index = await enqueue(s, () => reserveSlot($, s, armed, epoch, deadline, kind))
    if (typeof index !== 'number' || s.epoch !== epoch) return
    const ping: Ping = {
      nonce: newNonce(),
      kind,
      index,
      offersStop: kind === 'ping' || index > 0,
      session: armed.session,
      stretch: armed.stretch,
      phase: 'submitted',
      turnId: undefined,
      userCame: false,
    }
    // A prompt still unmatched never started (its queue was cleared): this one replaces it, and its slot stays spent.
    s.ping = ping
    try {
      const result = await $.prompt.submit({ text: kind === 'ping' ? pingText(ping.nonce) : reminderText(ping.nonce, ping.offersStop) })
      if (result.drop !== undefined && s.ping === ping && ping.phase === 'submitted') {
        s.ping = undefined
        note($, s, `${label(ping)} dropped: ${result.drop}`)
      }
    } catch (err) {
      if (s.ping === ping && ping.phase === 'submitted') s.ping = undefined
      note($, s, `${label(ping)} failed: ${describeError(err)}`)
    }
  } catch (err) {
    note($, s, `${kind} error: ${describeError(err)}`)
  }
}

/**
 * The prompts after the warn timer's: one timer, `lead` after this arm (pings 2 and on, or a reminder the warn timer could not send). Any later main-loop arm replaces it and a return cancels it.
 * Only after this stretch's warning acted (`warned`), and not while a warn timer is pending: that timer sends the first prompt.
 */
async function schedulePing($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<void> {
  const kw = await readKeepwarm($, s)
  const kind = nextKind(kw)
  if (kind === undefined || !kw.warned || armed.at.warn !== undefined) return
  if (kw.session !== armed.session || kw.stretch !== armed.stretch || kw.lead <= 0) return
  const due = (await $.clock.now()) + kw.lead
  if (s.epoch !== epoch || s.armed !== armed) return
  s.pingTimer?.cancel()
  s.pingTimer = $.clock.after(kw.lead, () => {
    void (async () => {
      try {
        if (s.armed !== armed || s.epoch !== epoch) return
        await runPing($, s, armed, epoch, due)
      } catch (err) {
        note($, s, `ping timer error: ${describeError(err)}`)
      }
    })()
  })
  note($, s, `${kind === 'reminder' ? 'state reminder' : `ping ${kw.sent + 1}`} in ${Math.round(kw.lead / 1000)}s`)
}

function label(ping: Ping): string {
  return ping.kind === 'ping' ? `ping ${ping.index}` : 'state reminder'
}

/**
 * The ping's or reminder's own turn ended: record how it went, and on a stop end this stretch's pings. True when the reminder chose
 * `compact-now`: the pings are already ended, so nothing races the compaction, and a vetoed one does not restart them.
 */
async function finishPing($: EngineInterface, s: State, ping: Ping, reason: string, answer: string): Promise<boolean> {
  if (reason !== 'answer') {
    note($, s, `${label(ping)} ended: ${reason}`)
    return false
  }
  const choice = parseChoice(answer)
  if (choice === 'compact-now' && ping.kind === 'reminder') {
    await endPings($, s, ping.session, ping.stretch, 'compact-now')
    return true
  }
  if (choice === 'stop-waiting' && ping.offersStop) {
    await endPings($, s, ping.session, ping.stretch, `${label(ping)} replied stop-waiting`)
  } else note($, s, `${label(ping)} continued`)
  return false
}

/**
 * Compacts the conversation the reminder just brought up to date, once the box has been quiet for `QUIET_MS` after the last main-loop turn's end and the last
 * prompt change: the call must land between turns, and the arm's latency is not what guarantees that. No deadline: the reminder turn just warmed the cache.
 * Gives up when the user came back, the box holds a draft (someone is there, and an unbounded wait could compact a cold cache), or a main-loop turn is running. Runs outside the CLI queue:
 * a compaction takes a minute and must not hold `return` or `fire` behind it.
 */
async function compactNow($: EngineInterface, s: State, ping: Ping, epoch: number): Promise<void> {
  try {
    for (;;) {
      if (ping.userCame || s.epoch !== epoch) return note($, s, 'compact-now skipped: the user came back')
      if (s.running) return note($, s, 'compact-now skipped: a turn is running')
      if (s.compacting) return note($, s, 'compact-now skipped: another compaction is running')
      const now = await $.clock.now()
      const changedAt = await s.lastChange
      const turnEnd = await s.lastTurnEnd
      const { text } = await $.prompt.read()
      if (ping.userCame || s.epoch !== epoch) return note($, s, 'compact-now skipped: the user came back')
      if (text !== '') return note($, s, 'compact-now skipped: the prompt box holds a draft')
      if (now - Math.max(changedAt, turnEnd) >= QUIET_MS) break
      await $.clock.sleep(HOLD_POLL_MS)
    }
    const result = await $.session.compact({ instructions: COMPACT_INSTRUCTIONS })
    note($, s, result.skip === undefined ? 'compacted' : `compaction skipped: ${result.skip}`)
  } catch (err) {
    note($, s, `compaction failed: ${describeError(err)}`)
  }
}

// -- return ----------------------------------------------------------------

async function returnTask($: EngineInterface, s: State, sessionId: Promise<string | undefined>): Promise<void> {
  if (!s.mayBeOpen) return // no arm reached core since the last return: nothing to close, skip the ~1 s call
  const session = await sessionId
  if (session === undefined) return
  s.mayBeOpen = false
  const reply = await cli($, s, ['return', '--harness', HARNESS, '--session', session, '--user-prompt'])
  const problem = failure(reply)
  if (problem !== undefined) {
    s.mayBeOpen = true
    note($, s, `return failed: ${problem}`)
  } else if (isRecord(reply) && reply.stretch_closed === true) {
    note($, s, 'return: stretch closed')
  }
}

// -- permission prompt -----------------------------------------------------

async function needsPermissionTask($: EngineInterface, s: State, message: string): Promise<void> {
  try {
    const reply = await cli($, s, [
      'needs-permission', '--harness', HARNESS, `--message=${message}`,
    ])
    const problem = failure(reply)
    if (problem !== undefined) note($, s, `needs-permission failed: ${problem}`)
  } catch (err) {
    note($, s, `needs-permission error: ${describeError(err)}`)
  }
}

/** The user is back. Cancels timers at once (synchronously), then closes the stretch in core. */
function userReturned($: EngineInterface, s: State): void {
  s.epoch += 1
  cancelTimers(s)
  s.armed = undefined
  if (s.ping !== undefined) s.ping.userCame = true
  // Read the id now, not when the queued call runs: `/clear` changes it in between.
  const sessionId = $.session.id().catch(() => undefined)
  void enqueue(s, () => returnTask($, s, sessionId))
}

// -- timers -----------------------------------------------------------------

async function onTimer($: EngineInterface, s: State, stage: Stage, armed: Armed, epoch: number): Promise<void> {
  try {
    if (s.armed !== armed || s.epoch !== epoch) return
    const args = ['fire', stage, ...who(armed), '--anchor', String(armed.anchor)]
    const reply = await enqueue(s, () => cli($, s, args))
    const problem = failure(reply)
    if (problem !== undefined || !isRecord(reply)) {
      note($, s, `fire ${stage} failed: ${problem ?? 'unexpected reply'}`)
      return
    }
    note($, s, `fire ${stage}: ${String(reply.decision)} (${String(reply.reason)})`)
    armed.fired[stage] = reply.decision === 'act' ? 'act' : `skip: ${String(reply.reason)}`
    // The notice is already sent; the first ping follows it. Waiting for a quiet box must not hold the queue.
    const dueAt = armed.at.warn
    if (stage === 'warn' && reply.decision === 'act' && dueAt !== undefined) {
      await enqueue(s, async () => {
        const kw = await stretchRecord($, s, armed.session, armed.stretch)
        await $.state.set(IDLE_KEEPWARM, { ...kw, warned: true, reminder: reply.remind === true })
      })
      // Core says `remind` when the session has a work item and `[idle] state_reminder` is on: the reminder is owed until submitted, so a turn
      // running now does not lose it (the next arm sends it).
      void runPing($, s, armed, epoch, dueAt)
    }
  } catch (err) {
    note($, s, `timer ${stage} error: ${describeError(err)}`)
  }
}

// -- session start ---------------------------------------------------------

async function startTask($: EngineInterface, s: State, epoch: number): Promise<void> {
  if (!(await gate($, s))) return
  try {
    await $.command.register({ name: COMMAND, description: 'Show Meridian idle stretch and timers' })
    await $.command.register({ name: KEEPWARM_COMMAND, description: 'Toggle keepwarm, or pass on|off' })
  } catch (err) {
    note($, s, `command registration failed: ${describeError(err)}`)
  }
  try {
    await recover($, s, await $.session.id(), epoch)
  } catch (err) {
    note($, s, `recover error: ${describeError(err)}`)
  }
}

// -- /meridian-idle --------------------------------------------------------

async function statusText($: EngineInterface, s: State): Promise<string> {
  // The host already prefixes a plugin command's output with the plugin's name.
  if (s.inert) return 'inactive for this session'
  const now = await $.clock.now()
  const lines: string[] = []
  const armed = s.armed
  if (armed === undefined) {
    lines.push('no stretch armed')
  } else {
    lines.push(`stretch ${armed.stretch}, anchor ${armed.anchor}`)
    for (const stage of STAGES) {
      const when = armed.at[stage]
      if (when === undefined) continue
      const fired = armed.fired[stage]
      const timing = fired !== undefined ? `fired (${fired})` : when <= now ? 'due' : `in ${Math.round((when - now) / 1000)}s`
      lines.push(`  ${stage.padEnd(7)} ${timing}`)
    }
  }
  lines.push(await keepwarmLine($, s))
  if (s.log.length > 0) {
    lines.push('log:')
    for (const line of s.log.slice(-8)) lines.push(`  ${line}`)
  }
  return lines.join('\n')
}

// -- registration ----------------------------------------------------------

export const register: Register = on => {
  const s = newState()

  on('session.start', async ($, e, next) => {
    if (e.isInteractive === false || e.surface === null) {
      s.inert = true
    } else {
      // Off the startup path: `meridian` takes about a second.
      const epoch = s.epoch
      void enqueue(s, () => startTask($, s, epoch))
    }
    return next(e)
  })

  // Subagent, teammate and compaction-fork turns raise turn.start too, with agentId set. Only the main loop's
  // start retires the Stop list that its own turn.complete will use.
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      s.stopTasks = undefined
      s.running = true
      // The ping may queue behind a running turn, so its turn is found by the nonce in its text, not by order.
      const ping = s.ping
      if (ping?.phase === 'submitted' && e.text.includes(ping.nonce)) {
        ping.phase = 'running'
        ping.turnId = e.turnId
      }
    }
    return next(e)
  })

  // The main loop's Stop fires just before its turn.complete and is the only place the host lists background work.
  on('classic.Stop', async ($, e, next) => {
    s.stopTasks = e.background_tasks
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // A subagent's turn carries agentId; only the main loop's end is the user-visible idle.
    if (e.agentId === undefined) {
      s.running = false
      s.lastTurnEnd = $.clock.now()
      void s.lastTurnEnd.catch(() => undefined)
    }
    if (e.agentId === undefined && !s.inert) {
      const epoch = s.epoch
      const tasks = s.stopTasks
      s.stopTasks = undefined
      const ping = s.ping?.turnId === e.turnId ? s.ping : undefined
      if (ping !== undefined) s.ping = undefined
      else s.lastAnswer = e.answer
      void enqueue(s, async () => {
        const compact = ping !== undefined && (await finishPing($, s, ping, e.reason, e.answer))
        // The ping's reply must not become the your-turn notice's excerpt.
        await armTask($, s, epoch, ping !== undefined ? s.lastAnswer : e.answer, tasks)
        if (compact && ping !== undefined) void compactNow($, s, ping, epoch)
      })
    }
    return next(e)
  })

  on('classic.Notification', async ($, e, next) => {
    if (!s.inert && e.notification_type === 'permission_prompt') {
      // This signal is independent of the idle timeline: do not queue it behind state calls.
      void needsPermissionTask($, s, e.message)
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (!s.inert && isUserOrigin(e.origin)) {
      s.lastUserText = e.text
      userReturned($, s)
    }
    return next(e)
  })

  // While the ping's turn runs the user is away and only asked to keep the cache warm, not to resume work.
  on('tool.check', async ($, e, next) => {
    const ping = s.ping
    // Other plugins' permission queries carry no tool_use_id and are not calls to deny.
    // The reminder turn must write the work item, so only a ping's turn is denied.
    if (e.tool_use_id === undefined || e.agentId !== undefined || e.isReadOnly === true || ping?.phase !== 'running' || ping.kind !== 'ping' || ping.userCame) return next(e)
    return {
      decision: 'deny',
      reason:
        'The user is away. meridian-idle only asked you to keep the cache warm: reply briefly and change nothing. To stop waiting for the user and let the cache go cold, reply with exactly stop-waiting.',
    }
  })

  // The user touching the prompt box: typing and deletion arrive as prompt.edit, every other write as prompt.change.
  // Both run per edit, so they only record the time.
  on('prompt.edit', async ($, e, next) => {
    s.lastChange = $.clock.now()
    void s.lastChange.catch(() => undefined)
    return next(e)
  })
  on('prompt.change', async ($, e, next) => {
    s.lastChange = $.clock.now()
    void s.lastChange.catch(() => undefined)
    return next(e)
  })

  // Typed slash commands never reach prompt.submit. `/meridian-idle` and `/keepwarm` do not count as a return.
  on('command.run', { command: COMMAND }, async ($, e, next) => {
    void next
    if (e.args.trim() !== '') return { text: 'usage: /meridian-idle\nkeepwarm: /keepwarm [on|off]' }
    return { text: await statusText($, s) }
  })

  on('command.run', { command: KEEPWARM_COMMAND }, async ($, e, next) => {
    void next
    const word = e.args.trim()
    if (word !== '' && word !== 'on' && word !== 'off') {
      return { text: 'usage: /keepwarm [on|off]' }
    }
    await enqueue(s, async () => {
      const kw = await readKeepwarm($, s)
      await $.state.set(IDLE_KEEPWARM, { ...kw, on: word === '' ? !kw.on : word === 'on' })
    })
    return { text: await keepwarmLine($, s) }
  })

  on('command.run', async ($, e, next) => {
    if (!s.inert && e.command !== COMMAND && e.command !== KEEPWARM_COMMAND && isUserOrigin(e.origin)) {
      s.lastUserText = `/${e.command}${e.args ? ` ${e.args}` : ''}`
      userReturned($, s)
    }
    return next(e)
  })

  // A compaction replaces the conversation the pings were keeping cached, so a ping now would prefill the new summary
  // and add a turn. `precompute` only computes a summary to keep; nothing is replaced yet. A manual /compact has already
  // counted as a return (its command.run), so this acts on the auto and plugin compactions of a still-armed stretch.
  on('session.compact', async ($, e, next) => {
    const armed = s.armed
    if (s.inert || e.agentId !== undefined || e.trigger === 'precompute' || armed === undefined) return next(e)
    // `session.compact` fires before the compaction, and `next` resolves to its outcome: a skip (a PreCompact hook
    // blocked it) leaves the conversation and its cache, so only a result with messages ends the pings.
    s.compacting = true
    let result: Awaited<ReturnType<typeof next>>
    try {
      result = await next(e)
    } finally {
      s.compacting = false
    }
    if ('messages' in result) {
      s.pingTimer?.cancel()
      s.pingTimer = undefined
      // Queued before the next reservation, so a ping waiting in holdForUser or a timer that already fired finds the cap.
      void enqueue(s, () => endPings($, s, armed.session, armed.stretch, `compaction (${e.trigger})`))
    }
    return result
  })

  on('session.end', async ($, e, next) => {
    s.epoch += 1
    cancelTimers(s)
    s.armed = undefined
    return next(e)
  })
}
