import type { ClassicHookInputs, EngineInterface, Register, Timer } from 'claude-code'

import {
  FRESH,
  PING_CAP,
  appended,
  excerpt,
  isStop,
  pingText,
  recordLines,
  type Keepwarm,
  type Outcome,
} from './keepwarm'

// Meridian's Claude idle sensor.
//
// This module senses three things and hands every decision to `meridian idle`:
//   idle    a main-loop turn finished        -> `idle arm` with the work still running, then one timer per stage
//   return  the user typed or sent something -> `idle return`, timers cancelled
//   permission  Claude still awaits approval -> `idle needs-permission`, with no state change
//   keepwarm  the cache warning fired and the user opted in -> up to three prompts that keep the cache warm (see keepwarm.ts)
// Notifications belong to core; this file never sends one, reads
// meridian config or env, or writes under `~/.meridian`. Nothing here writes to the terminal: diagnostics go to the
// plugin's own `$.store` key `log`, and `/meridian-idle` prints them on request.

type Stage = 'push' | 'warn'

const STAGES: readonly Stage[] = ['push', 'warn']
const HARNESS = 'claude'
const COMMAND = 'meridian-idle'
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
/** A chained ping this late means the machine slept and the cache may be cold: skip it. */
const LATE_PING_MS = 60_000
/** How long a ping waits for a quiet box when core's lead is unknown. */
const HOLD_FALLBACK_MS = 9 * 60_000
const KEEPWARM_LOG = 'keepwarm'
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

/** The ping that was submitted and has not completed. Only one exists at a time. */
type Ping = {
  nonce: string
  index: number
  session: string
  stretch: number
  /** `submitted`: no main-loop turn carrying the nonce has started yet. `running`: that turn is in flight (`turnId`). */
  phase: 'submitted' | 'running'
  turnId: string | undefined
  denied: string[]
  /** The user typed during the ping turn: tools are theirs to steer again. */
  userCame: boolean
}

type State = {
  /** Print mode or no surface: the whole session is ignored. */
  inert: boolean
  /** Memoised `idle config --interactive` verdict. */
  gate: Promise<boolean> | undefined
  armed: Armed | undefined
  timers: Timer[]
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
  /** The last main-loop answer that was not a ping's: a ping turn's arm sends it, so the your-turn notice never reads the ping's reply. */
  lastAnswer: string | undefined
  /** When the prompt box was last touched, as `$.clock.now()` answered it; kept as the pending answer so hooks never wait on it. */
  lastChange: Promise<number>
}

function newState(): State {
  return {
    inert: false,
    gate: undefined,
    armed: undefined,
    timers: [],
    epoch: 0,
    mayBeOpen: false,
    queue: Promise.resolve(),
    log: [],
    lastUserText: undefined,
    stopTasks: undefined,
    ping: undefined,
    lastAnswer: undefined,
    lastChange: Promise.resolve(Number.NEGATIVE_INFINITY),
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
  await adopt($, s, { session, stretch, anchor, at, fired: {} }, epoch)
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
    await trackStretch($, session, stretch, warnAt === undefined ? undefined : warnAt - (await $.clock.now()))
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

async function readKeepwarm($: EngineInterface): Promise<Keepwarm> {
  return (await $.state.get(IDLE_KEEPWARM)).value ?? FRESH
}

/** `kw` as it stands for this stretch: a different session or core stretch starts the count and lead over. */
function inStretch(kw: Keepwarm, session: string, stretch: number): Keepwarm {
  return kw.session === session && kw.stretch === stretch ? kw : { ...kw, session, stretch, sent: 0, lead: 0 }
}

/** An arm reached core: record which stretch it belongs to and, when it placed the warning, the lead. */
async function trackStretch($: EngineInterface, session: string, stretch: number, lead: number | undefined): Promise<void> {
  const before = await readKeepwarm($)
  const kw = inStretch(before, session, stretch)
  const next = lead !== undefined && lead > 0 ? { ...kw, lead } : kw
  if (next.session !== before.session || next.stretch !== before.stretch || next.lead !== before.lead) {
    await $.state.set(IDLE_KEEPWARM, next)
  }
}

function pingsOpen(kw: Keepwarm): boolean {
  return kw.on && kw.sent < PING_CAP
}

/** Appends the ping's record to the experiment log. Runs inside the queue; from outside it use `logFailed`. */
async function appendRecord(
  $: EngineInterface,
  s: State,
  ping: Ping,
  outcome: Outcome,
  reply: string,
  why?: string,
): Promise<void> {
  const record = {
    at: new Date().toISOString(),
    session: ping.session,
    stretch: ping.stretch,
    ping: ping.index,
    outcome,
    denied: ping.denied,
    reply: excerpt(reply),
    ...(why !== undefined ? { why } : {}),
  }
  // Another session may append at once and one record can be lost; pings are tens of minutes apart.
  await $.store.set(KEEPWARM_LOG, appended(await $.store.get(KEEPWARM_LOG), record))
  note($, s, `ping ${ping.index} ${outcome}${why !== undefined ? ` (${why})` : ''}${ping.denied.length > 0 ? ` denied ${ping.denied.join(',')}` : ''}`)
}

function logFailed($: EngineInterface, s: State, ping: Ping, why: string): Promise<unknown> {
  return enqueue(s, () => appendRecord($, s, ping, 'failed', '', why))
}

/** Resolves true once the prompt box is empty and untouched for `QUIET_MS`; false if the user returned or `limitMs` passed first. */
async function holdForUser($: EngineInterface, s: State, epoch: number, limitMs: number): Promise<boolean> {
  const start = await $.clock.now()
  for (;;) {
    if (s.epoch !== epoch) return false
    const now = await $.clock.now()
    const changedAt = await s.lastChange
    const { text } = await $.prompt.read()
    if (s.epoch !== epoch) return false
    if (text === '' && now - changedAt >= QUIET_MS) return true
    if (now - start >= limitMs) return false
    await $.clock.sleep(HOLD_POLL_MS)
  }
}

function newNonce(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

/** Takes the next ping slot of this stretch; undefined when keepwarm is off, the pings are used up, or the user returned. */
async function reserveSlot($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<number | undefined> {
  const kw = inStretch(await readKeepwarm($), armed.session, armed.stretch)
  if (s.epoch !== epoch || !pingsOpen(kw)) return undefined
  await $.state.set(IDLE_KEEPWARM, { ...kw, sent: kw.sent + 1 })
  return kw.sent + 1
}

/**
 * Sends one keep-warm prompt for `armed`'s stretch, once the box has been quiet. The count is taken before the
 * submit so that a reload or a failure can only cost a ping, never add one.
 */
async function runPing($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<void> {
  try {
    const kw = inStretch(await readKeepwarm($), armed.session, armed.stretch)
    if (!pingsOpen(kw)) return
    const limit = kw.lead > LATE_PING_MS ? kw.lead - LATE_PING_MS : HOLD_FALLBACK_MS
    if (!(await holdForUser($, s, epoch, limit))) {
      note($, s, 'ping skipped: user is at the keyboard')
      return
    }
    const index = await enqueue(s, () => reserveSlot($, s, armed, epoch))
    if (typeof index !== 'number' || s.epoch !== epoch) return
    const lost = s.ping
    const ping: Ping = {
      nonce: newNonce(),
      index,
      session: armed.session,
      stretch: armed.stretch,
      phase: 'submitted',
      turnId: undefined,
      denied: [],
      userCame: false,
    }
    // A ping still unmatched never started (its queue was cleared); its slot stays spent.
    if (lost !== undefined) void logFailed($, s, lost, 'turn never started')
    s.ping = ping
    try {
      const result = await $.prompt.submit({ text: pingText(ping.nonce) })
      if (result.drop !== undefined && s.ping === ping && ping.phase === 'submitted') {
        s.ping = undefined
        await logFailed($, s, ping, `dropped: ${result.drop}`)
      }
    } catch (err) {
      if (s.ping === ping && ping.phase === 'submitted') s.ping = undefined
      await logFailed($, s, ping, describeError(err))
    }
  } catch (err) {
    note($, s, `ping error: ${describeError(err)}`)
  }
}

/** Pings 2 and 3: one timer, `lead` after this arm. Any later main-loop arm replaces it and a return cancels it. */
async function schedulePing($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<void> {
  const kw = await readKeepwarm($)
  if (!pingsOpen(kw) || kw.sent < 1 || kw.session !== armed.session || kw.stretch !== armed.stretch || kw.lead <= 0) return
  const due = (await $.clock.now()) + kw.lead
  if (s.epoch !== epoch || s.armed !== armed) return
  s.timers.push(
    $.clock.after(kw.lead, () => {
      void (async () => {
        try {
          if (s.armed !== armed || s.epoch !== epoch) return
          if ((await $.clock.now()) - due > LATE_PING_MS) {
            note($, s, 'ping skipped: timer ran late, the cache may be cold')
            return
          }
          await runPing($, s, armed, epoch)
        } catch (err) {
          note($, s, `ping timer error: ${describeError(err)}`)
        }
      })()
    }),
  )
  note($, s, `ping ${kw.sent + 1} in ${Math.round(kw.lead / 1000)}s`)
}

/** The ping's own turn ended: record how it went and, on a stop, end this stretch's pings. */
async function finishPing($: EngineInterface, s: State, ping: Ping, reason: string, answer: string): Promise<void> {
  if (reason !== 'answer') return appendRecord($, s, ping, 'failed', answer, `turn ${reason}`)
  if (!isStop(answer)) return appendRecord($, s, ping, 'continued', answer)
  const kw = await readKeepwarm($)
  if (kw.session === ping.session && kw.stretch === ping.stretch) await $.state.set(IDLE_KEEPWARM, { ...kw, sent: PING_CAP })
  return appendRecord($, s, ping, 'stopped', answer)
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
    if (stage === 'warn' && reply.decision === 'act') void runPing($, s, armed, epoch)
  } catch (err) {
    note($, s, `timer ${stage} error: ${describeError(err)}`)
  }
}

// -- session start ---------------------------------------------------------

async function startTask($: EngineInterface, s: State, epoch: number): Promise<void> {
  if (!(await gate($, s))) return
  try {
    await $.command.register({ name: COMMAND, description: 'Show Meridian idle stretch and timers; keepwarm on|off pings the model before the cache expires' })
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
  const kw = await readKeepwarm($)
  const sent = armed !== undefined && kw.session === armed.session && kw.stretch === armed.stretch ? kw.sent : 0
  lines.push(`keepwarm: ${kw.on ? 'on' : 'off'}, ${sent}/${PING_CAP} pings this stretch`)
  lines.push(...recordLines(await $.store.get(KEEPWARM_LOG)))
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
    if (e.agentId === undefined && !s.inert) {
      const epoch = s.epoch
      const tasks = s.stopTasks
      s.stopTasks = undefined
      const ping = s.ping?.turnId === e.turnId ? s.ping : undefined
      if (ping !== undefined) s.ping = undefined
      else s.lastAnswer = e.answer
      void enqueue(s, async () => {
        if (ping !== undefined) await finishPing($, s, ping, e.reason, e.answer)
        // The ping's reply must not become the your-turn notice's excerpt.
        await armTask($, s, epoch, ping !== undefined ? s.lastAnswer : e.answer, tasks)
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
    if (e.tool_use_id === undefined || e.agentId !== undefined || e.isReadOnly === true || ping?.phase !== 'running' || ping.userCame) return next(e)
    if (!ping.denied.includes(e.tool)) ping.denied.push(e.tool)
    return {
      decision: 'deny',
      reason: 'The user is away. meridian-idle only asked you to keep the cache warm: reply briefly and change nothing.',
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

  // Typed slash commands never reach prompt.submit. `/meridian-idle` itself is a read-only look at the timers and does not count as a return.
  on('command.run', { command: COMMAND }, async ($, e, next) => {
    void next
    const [word, value] = e.args.trim().split(/\s+/)
    if (word === 'keepwarm' && (value === 'on' || value === 'off')) {
      await enqueue(s, async () => $.state.set(IDLE_KEEPWARM, { ...(await readKeepwarm($)), on: value === 'on' }))
    } else if (word !== undefined && word !== '') {
      return { text: 'usage: /meridian-idle [keepwarm on|off]' }
    }
    return { text: await statusText($, s) }
  })

  on('command.run', async ($, e, next) => {
    if (!s.inert && e.command !== COMMAND && isUserOrigin(e.origin)) {
      s.lastUserText = `/${e.command}${e.args ? ` ${e.args}` : ''}`
      userReturned($, s)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    s.epoch += 1
    cancelTimers(s)
    s.armed = undefined
    return next(e)
  })
}
