import type { EngineInterface, Register, Timer } from 'claude-code'

// Meridian's Claude idle sensor.
//
// This module senses two things and hands every decision to `meridian idle`:
//   idle    a main-loop turn finished        -> `idle arm`, then one timer per stage
//   return  the user typed or sent something -> `idle return`, timers cancelled
// It acts on exactly one answer: `{decision: "act"}` from `idle fire compact`,
// and then only from a timer callback, because `$.session.compact()` is
// rejected inside a `command.run` hook and the mod cannot see its own
// compaction (probes/P0a.md, Q2). Push and warn notifications belong to core;
// this file never sends one, reads meridian config or env, or writes under
// `~/.meridian`. Nothing here writes to the terminal: diagnostics go to the
// plugin's own `$.store` key `log`, and `/meridian-idle` prints them on request.

type Stage = 'push' | 'warn' | 'compact'

const STAGES: readonly Stage[] = ['push', 'warn', 'compact']
const HARNESS = 'claude'
const COMMAND = 'meridian-idle'
const CLI_TIMEOUT_MS = 20_000
const LOG_LIMIT = 100
const EXCERPT_CHAR_CAP = 4_000

type Json = Record<string, unknown>

/** One open stretch as the mod knows it: the ids `fire` and `done` need, and the absolute stage times. */
type Armed = {
  session: string
  stretch: number
  anchor: number
  at: Partial<Record<Stage, number>>
  /** What core answered when each stage's timer fired, for `/meridian-idle`. */
  fired: Partial<Record<Stage, string>>
}

type State = {
  /** `session.start` ran, so its `isInteractive`/`surface` verdict is authoritative. */
  startSeen: boolean
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
  /** A main-loop turn is running (turn.start seen, its turn.complete not yet): `$.session.compact()` would be rejected. */
  busy: boolean
  /** Serialises the short `meridian idle` calls so arm/return/fire/done reach core in hook order. */
  queue: Promise<unknown>
  log: string[]
  /** Last prompt typed by the human; tool results and injected hook turns never enter here. */
  lastUserText: string | undefined
}

function newState(): State {
  return {
    startSeen: false,
    inert: false,
    gate: undefined,
    armed: undefined,
    timers: [],
    epoch: 0,
    mayBeOpen: false,
    busy: false,
    queue: Promise.resolve(),
    log: [],
    lastUserText: undefined,
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
  if (s.inert) return false
  if (!s.startSeen) {
    // Loaded after `session.start` (a plugin install or reload that skipped it): ask the host directly.
    try {
      if ((await $.session.surfaces()).length === 0) {
        s.inert = true
        note($, s, 'inert: no surface (print mode)')
        return false
      }
    } catch (err) {
      note($, s, `inert: cannot read surfaces: ${describeError(err)}`)
      // Surface discovery can fail transiently during startup or reload. Keep
      // the gate fail-closed for this call, but let the next event retry it.
      s.gate = undefined
      return false
    }
  }
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

async function runningAgents($: EngineInterface): Promise<number | undefined> {
  try {
    return (await $.agent.list()).filter(agent => agent.status === 'running').length
  } catch {
    return undefined
  }
}

async function armTask($: EngineInterface, s: State, epoch: number, assistantText?: string): Promise<void> {
  if (!(await gate($, s))) return
  try {
    // The main loop can finish while a subagent still runs; the hand-back turn ends the main loop again, and arms then.
    const running = await runningAgents($)
    if (running !== undefined && running > 0) {
      note($, s, `arm skipped: ${running} subagent(s) still running`)
      return
    }
    const session = await $.session.id()
    const cwd = await $.session.cwd()
    const args = ['arm', '--harness', HARNESS, '--session', session, '--cwd', cwd]
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
      return // same stretch and anchor: core absorbed this arm (compaction window, already compacted); keep our timers
    }
    const at = readSchedule(reply)
    if (Object.keys(at).length === 0 && typeof reply.reason === 'string') {
      // An absorbed arm for a stretch we do not know yet carries no deadlines: take them from the store.
      await recover($, s, session, epoch)
      return
    }
    await adopt($, s, { session, stretch, anchor, at, fired: {} }, epoch)
  } catch (err) {
    note($, s, `arm error: ${describeError(err)}`)
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

/** The user is back. Cancels timers at once (synchronously), then closes the stretch in core. */
function userReturned($: EngineInterface, s: State): void {
  s.epoch += 1
  cancelTimers(s)
  s.armed = undefined
  // Read the id now, not when the queued call runs: `/clear` changes it in between.
  const sessionId = $.session.id().catch(() => undefined)
  void enqueue(s, () => returnTask($, s, sessionId))
}

// -- timers: fire, and act on compact --------------------------------------

async function draftState($: EngineInterface): Promise<'yes' | 'no' | 'unknown'> {
  try {
    return (await $.prompt.read()).text === '' ? 'no' : 'yes'
  } catch {
    return 'unknown'
  }
}

async function contextTokens($: EngineInterface): Promise<number | undefined> {
  try {
    return asNumber((await $.session.usage()).context.tokens)
  } catch {
    return undefined
  }
}

async function autoCompactOff($: EngineInterface): Promise<boolean> {
  try {
    const rows = await $.config.list()
    return rows.some(row => row.key === 'autoCompact' && row.value === false)
  } catch {
    return false
  }
}

/** Facts `idle fire compact` needs. A fact the host will not give is omitted, so core applies its own unknown-fact rule. */
async function compactFacts($: EngineInterface, busy: boolean): Promise<string[]> {
  const [draft, agents, tokens, autoOff] = await Promise.all([
    draftState($),
    runningAgents($),
    contextTokens($),
    autoCompactOff($),
  ])
  const facts = ['--draft', draft]
  // A running turn, or agents we cannot list, is not "idle": report busy so core skips instead of compacting under them.
  if (busy || agents === undefined) facts.push('--busy')
  if (agents !== undefined) facts.push('--agents-running', String(agents))
  if (tokens !== undefined) facts.push('--context-tokens', String(tokens))
  if (autoOff) facts.push('--harness-autocompact-off')
  return facts
}

type Outcome = { result: 'ok' | 'failed' | 'vetoed'; reason?: string }

function compacted(tokensBefore: number | undefined, tokensAfter: number | undefined): Outcome {
  if (tokensBefore === undefined || tokensAfter === undefined) return { result: 'ok' }
  return { result: 'ok', reason: `${Math.round(tokensBefore / 1000)}k -> ${Math.round(tokensAfter / 1000)}k tokens` }
}

/** Core said "act" and has claimed the stage, so exactly one `done` must follow whatever happens here. */
async function compactNow($: EngineInterface, s: State, armed: Armed, epoch: number): Promise<Outcome> {
  if (s.epoch !== epoch) return { result: 'vetoed', reason: 'user returned' }
  // The fire call took ~1 s; a draft typed since then must still win.
  if ((await draftState($)) !== 'no') return { result: 'vetoed', reason: 'draft' }
  if (s.epoch !== epoch) return { result: 'vetoed', reason: 'user returned' }
  try {
    const result = await $.session.compact()
    if (result.skip !== undefined) return { result: 'vetoed', reason: result.skip }
    return compacted(result.tokensBefore, result.tokensAfter)
  } catch (err) {
    return { result: 'failed', reason: describeError(err) }
  }
}

async function onTimer($: EngineInterface, s: State, stage: Stage, armed: Armed, epoch: number): Promise<void> {
  try {
    if (s.armed !== armed || s.epoch !== epoch) return
    const facts = stage === 'compact' ? await compactFacts($, s.busy) : []
    if (s.armed !== armed || s.epoch !== epoch) return
    const args = ['fire', stage, ...who(armed), '--anchor', String(armed.anchor), ...facts]
    const reply = await enqueue(s, () => cli($, s, args))
    const problem = failure(reply)
    if (problem !== undefined || !isRecord(reply)) {
      note($, s, `fire ${stage} failed: ${problem ?? 'unexpected reply'}`)
      return
    }
    note($, s, `fire ${stage}: ${String(reply.decision)} (${String(reply.reason)})`)
    armed.fired[stage] = reply.decision === 'act' ? 'act' : `skip: ${String(reply.reason)}`
    // Push and warn are core's job. Only a compact "act" asks anything of us.
    if (stage !== 'compact' || reply.decision !== 'act') return

    const outcome = await compactNow($, s, armed, epoch)
    const done = ['done', 'compact', ...who(armed), '--result', outcome.result]
    if (outcome.reason !== undefined) done.push('--reason', outcome.reason)
    const doneReply = await enqueue(s, () => cli($, s, done))
    note($, s, `compact ${outcome.result}${outcome.reason ? ` (${outcome.reason})` : ''}${failure(doneReply) ? `; done failed: ${failure(doneReply)}` : ''}`)
  } catch (err) {
    note($, s, `timer ${stage} error: ${describeError(err)}`)
  }
}

// -- session start ---------------------------------------------------------

async function startTask($: EngineInterface, s: State, epoch: number): Promise<void> {
  if (!(await gate($, s))) return
  try {
    await $.command.register({ name: COMMAND, description: 'Show Meridian idle stretch and timers' })
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
    s.startSeen = true
    if (e.isInteractive === false || e.surface === null) {
      s.inert = true
    } else {
      // Off the startup path: `meridian` takes about a second.
      const epoch = s.epoch
      void enqueue(s, () => startTask($, s, epoch))
    }
    return next(e)
  })

  // A subagent's run raises no turn.start, so this is always the main loop.
  on('turn.start', async ($, e, next) => {
    s.busy = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // A subagent's turn carries agentId; only the main loop's end is the user-visible idle.
    if (e.agentId === undefined && !s.inert) {
      s.busy = false
      const epoch = s.epoch
      void enqueue(s, () => armTask($, s, epoch, e.answer))
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

  // Typed slash commands never reach prompt.submit. `/meridian-idle` itself is a read-only look at the timers and does not count as a return.
  on('command.run', { command: COMMAND }, async ($, e, next) => {
    void next
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
