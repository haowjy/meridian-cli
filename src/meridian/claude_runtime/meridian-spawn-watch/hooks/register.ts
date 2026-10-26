import type { EngineInterface, Register } from 'claude-code'

// Meridian's Claude spawn watcher.
//
// When the lead's main loop goes idle, `meridian spawn collect` finds the background children it never
// waited on and blocks until their results are due; this module submits that text as a new turn and
// then tells Meridian the lead has it (`meridian spawn ack`). Which children are owed, when they are
// done, what the lead sees and what counts as consumed all live in core. This file only senses idle,
// runs the two commands, and keeps the delivery safe against the ways a turn can fail to happen.
// Nothing here writes to the terminal: diagnostics go to the plugin's own `$.store` key `log`, and
// `/meridian-spawn-watch` prints them on request.
//
// One idle stretch is one `Run`: a 2 s delay, the collector, a hold while the user types, the submit.
// A stretch begins only when a main-loop turn ends, never at session start: a resumed chat's prompt cache is
// cold, so a turn the user did not ask for would re-bill the whole context for a status ping. Results owed
// from before a crash or restart wait for the first turn the user starts. A main-loop `turn.start` aborts
// the run wherever it is; aborting the collector's read loop kills the child.
//
// Consumption is acked only once the lead has the text. Only one run exists at a time, so no collector
// runs while a delivery is held; the ids are claimed in memory when the prompt is submitted, and every
// collector started until the ack lands excludes them. The ack runs on the main-loop `turn.start` whose
// text carries the delivery's nonce. A dropped, queued or never-started prompt is never acked.

const COMMAND = 'meridian-spawn-watch'
/** The idle signal is a main-loop turn ending; a queued prompt starts its turn within this, so it costs no process. */
const IDLE_DELAY_MS = 2_000
/** The prompt box must be empty and untouched this long before a delivery starts a turn under the user. */
const QUIET_MS = 10_000
const HOLD_POLL_MS = 1_000
const ACK_TIMEOUT_MS = 20_000
const LOG_LIMIT = 100
/** Safety cap for the collector subprocess's JSON output. */
const STDOUT_CHAR_CAP = 1 << 20
const STDERR_CHAR_CAP = 500
const SPAWN_ID = /^[A-Za-z0-9][\w.-]*$/

type Phase = 'delay' | 'collecting' | 'holding' | 'submitting'

/** One idle stretch's work. Aborting it ends the delay, kills the collector, and ends the hold. */
type Run = { ctl: AbortController; phase: Phase }

/**
 * Ids handed to the lead and not yet recorded as consumed.
 *   submitted the prompt went to `$.prompt.submit`; no turn carrying the nonce has started
 *   acking    that turn started; the ack is running
 *   unacked   the ack failed; the ids stay excluded for the life of this module copy, because releasing them
 *             would redeliver the same results at every idle
 */
type Claim = { ids: string[]; phase: 'submitted' | 'acking' | 'unacked' }

type Reply = { kind: 'empty' } | { kind: 'delivery'; ids: string[]; text: string }

type State = {
  /** Print mode or no surface: the whole session is ignored. */
  inert: boolean
  run: Run | undefined
  claims: Map<string, Claim>
  /** When the box was last touched (`prompt.edit` or `prompt.change`), as `$.clock.now()` answered it. Kept as the pending answer so the hooks never wait on it. */
  lastChange: Promise<number>
  /** The last collector verdict, for `/meridian-spawn-watch`. Empty answers stay out of the log. */
  last: string
  log: string[]
}

function newState(): State {
  return {
    inert: false,
    run: undefined,
    claims: new Map(),
    lastChange: Promise.resolve(Number.NEGATIVE_INFINITY),
    last: 'no collect yet',
    log: [],
  }
}

// -- small helpers ---------------------------------------------------------

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Local wall-clock time of day: `/meridian-spawn-watch` shows it in a narrow column. */
function stamp(): string {
  return new Date().toTimeString().slice(0, 8)
}

function note($: EngineInterface, s: State, line: string): void {
  s.log.push(`${stamp()} ${line}`)
  if (s.log.length > LOG_LIMIT) s.log.splice(0, s.log.length - LOG_LIMIT)
  try {
    $.store.set('log', s.log).catch(() => undefined)
  } catch {
    // the log is a convenience; a store that refuses it is not the lead's problem
  }
}

function claimedIds(s: State): Set<string> {
  const ids = new Set<string>()
  for (const claim of s.claims.values()) for (const id of claim.ids) ids.add(id)
  return ids
}

function endRun(s: State): void {
  s.run?.ctl.abort()
  s.run = undefined
}

// -- collector -------------------------------------------------------------

function parseReply(stdout: string): Reply {
  const text = stdout.trim()
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`unparseable output: ${text.slice(0, 200)}`)
  }
  if (!isRecord(parsed)) throw new Error(`unexpected output: ${text.slice(0, 200)}`)
  if (parsed.kind === 'empty') return { kind: 'empty' }
  if (parsed.kind !== 'delivery') throw new Error(`unknown kind: ${String(parsed.kind)}`)
  const { ids, text: body } = parsed
  if (!Array.isArray(ids) || ids.length === 0 || !ids.every(id => typeof id === 'string' && SPAWN_ID.test(id))) {
    throw new Error('delivery without usable ids')
  }
  if (typeof body !== 'string') throw new Error('delivery without text')
  return { kind: 'delivery', ids: ids as string[], text: body }
}

/**
 * Runs `meridian spawn collect --exclude <claimed ids>` and returns its verdict, or undefined if the run was aborted.
 * Leaving the read loop is what kills the child, so an abort has to win the race against a read that may never come.
 */
async function collect($: EngineInterface, s: State, run: Run): Promise<Reply | undefined> {
  const signal = run.ctl.signal
  if (signal.aborted) return undefined
  const excluded = [...claimedIds(s)]
  const argv = ['meridian', 'spawn', 'collect', ...(excluded.length > 0 ? ['--exclude', excluded.join(',')] : [])]
  const gone = new Promise<'gone'>(resolve => signal.addEventListener('abort', () => resolve('gone'), { once: true }))
  const stream = $.process.spawn({ argv })
  let stdout = ''
  let stderr = ''
  try {
    for (;;) {
      const pull = stream.next()
      void pull.catch(() => undefined) // an abandoned read may still fail later
      const step = await Promise.race([pull, gone])
      if (step === 'gone') return undefined
      if (step.done) {
        const exit = step.value
        if (exit.code !== 0) {
          throw new Error(`collect exited ${exit.code ?? exit.signal}: ${stderr.trim().slice(-200)}`)
        }
        return parseReply(stdout)
      }
      if (step.value.stream === 'stdout') {
        stdout += step.value.text
        if (stdout.length > STDOUT_CHAR_CAP) throw new Error('collect output over the cap')
      } else {
        stderr = (stderr + step.value.text).slice(-STDERR_CHAR_CAP)
      }
    }
  } finally {
    void stream.return({ code: null, signal: null }).catch(() => undefined)
  }
}

// -- delivery --------------------------------------------------------------

/** Resolves once the prompt box is empty and untouched for `QUIET_MS`; rejects if the run is aborted first. */
async function holdForUser($: EngineInterface, s: State, run: Run): Promise<void> {
  for (;;) {
    run.ctl.signal.throwIfAborted()
    const now = await $.clock.now()
    const changedAt = await s.lastChange
    const { text } = await $.prompt.read()
    if (text === '' && now - changedAt >= QUIET_MS) return
    await $.clock.sleep(HOLD_POLL_MS, { signal: run.ctl.signal })
  }
}

function newNonce(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

async function deliver($: EngineInterface, s: State, run: Run, reply: Extract<Reply, { kind: 'delivery' }>): Promise<void> {
  try {
    run.phase = 'holding'
    await holdForUser($, s, run)
    // A turn can start during the hold's last awaits; submit is the one step that cannot be undone.
    run.ctl.signal.throwIfAborted()
  } catch (err) {
    note($, s, `held delivery of ${reply.ids.join(',')} released: ${run.ctl.signal.aborted ? 'a turn started' : describeError(err)}`)
    throw err
  }
  // From here the prompt is out of our hands: a main-loop turn.start aborts the run but cannot unsubmit it.
  const nonce = newNonce()
  const claim: Claim = { ids: reply.ids, phase: 'submitted' }
  s.claims.set(nonce, claim)
  run.phase = 'submitting'
  try {
    const result = await $.prompt.submit({ text: `[meridian-spawn-watch delivery ${nonce}]\n\n${reply.text}` })
    if (result.drop !== undefined) {
      s.claims.delete(nonce)
      note($, s, `delivery of ${reply.ids.join(',')} dropped: ${result.drop}`)
      return
    }
    note($, s, `submitted ${reply.ids.join(',')}`)
  } catch (err) {
    if (claim.phase === 'submitted') s.claims.delete(nonce)
    throw err
  }
}

async function idleRun($: EngineInterface, s: State, run: Run): Promise<void> {
  try {
    await $.clock.sleep(IDLE_DELAY_MS, { signal: run.ctl.signal })
    // A submitted prompt has entered by now (`submit` resolves once it starts its turn or queues behind a running one),
    // and a queued one would have started its turn. A claim still waiting for one was dropped (Esc, a cleared queue).
    for (const [nonce, claim] of s.claims) if (claim.phase === 'submitted') s.claims.delete(nonce)
    run.phase = 'collecting'
    const reply = await collect($, s, run)
    if (reply === undefined) return
    if (reply.kind === 'empty') {
      s.last = `empty at ${stamp()}`
      return
    }
    s.last = `delivery ${reply.ids.join(',')} at ${stamp()}`
    await deliver($, s, run, reply)
  } catch (err) {
    if (!run.ctl.signal.aborted) {
      s.last = `error at ${stamp()}`
      note($, s, `idle run failed: ${describeError(err)}`)
    }
  } finally {
    if (s.run === run) s.run = undefined
  }
}

async function ackTask($: EngineInterface, s: State, nonce: string, claim: Claim): Promise<void> {
  const argv = ['meridian', 'spawn', 'ack', ...claim.ids]
  try {
    const out = await $.process.run(argv, { timeoutMs: ACK_TIMEOUT_MS })
    if (out.exitCode === 0) {
      s.claims.delete(nonce)
      note($, s, `acked ${claim.ids.join(',')}`)
      return
    }
    note($, s, `ack ${claim.ids.join(',')} failed (exit ${out.exitCode}): ${out.stderr.trim().slice(0, 200)}`)
  } catch (err) {
    note($, s, `ack ${claim.ids.join(',')} failed: ${describeError(err)}`)
  }
  claim.phase = 'unacked'
}

// -- hook bodies -----------------------------------------------------------

/** A main-loop turn started: whatever the idle stretch was doing ends, and a delivery turn is acked. */
function mainTurnStarted($: EngineInterface, s: State, text: string): void {
  endRun(s)
  for (const [nonce, claim] of s.claims) {
    if (claim.phase === 'acking' || claim.phase === 'unacked' || !text.includes(nonce)) continue
    claim.phase = 'acking'
    void ackTask($, s, nonce, claim) // acks are idempotent set unions in core, so their order does not matter
  }
}

/** Begins an idle stretch, replacing the one in flight. Returns at once; the run's failures only reach the log. */
function startRun($: EngineInterface, s: State): void {
  endRun(s)
  const run: Run = { ctl: new AbortController(), phase: 'delay' }
  s.run = run
  void idleRun($, s, run)
}

function boxTouched($: EngineInterface, s: State): void {
  try {
    s.lastChange = $.clock.now()
    void s.lastChange.catch(() => undefined)
  } catch {
    // a missed timestamp only shortens the hold; it must never break typing
  }
}

function statusText(s: State): string {
  if (s.inert) return 'inactive for this session'
  const lines = [`idle run: ${s.run?.phase ?? 'none'}`, `last collect: ${s.last}`]
  for (const [nonce, claim] of s.claims) lines.push(`claim ${nonce}: ${claim.ids.join(',')} (${claim.phase})`)
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
    try {
      if (e.isInteractive === false || e.surface === null) s.inert = true
      else {
        // Off the startup path; a reload is a fresh module copy and registers again. No idle run starts here.
        $.command
          .register({ name: COMMAND, description: 'Show Meridian spawn-watch state' })
          .catch(err => note($, s, `command registration failed: ${describeError(err)}`))
      }
    } catch (err) {
      note($, s, `session.start error: ${describeError(err)}`)
    }
    return next(e)
  })

  // Subagent, teammate and compaction-fork turns raise turn.start and turn.complete too, with agentId set.
  // Only the main loop is the lead's idle; treating the others as turns would starve delivery while background agents run.
  on('turn.start', async ($, e, next) => {
    try {
      if (e.agentId === undefined) mainTurnStarted($, s, e.text)
    } catch (err) {
      note($, s, `turn.start error: ${describeError(err)}`)
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    try {
      // Not after an Esc (do not resume what the user stopped), a rate or context limit, or a refusal: the next
      // request would likely fail the same way. The results stay owed; the next normal idle delivers them.
      if (e.agentId === undefined && e.reason === 'answer' && !s.inert) startRun($, s)
    } catch (err) {
      note($, s, `turn.complete error: ${describeError(err)}`)
    }
    return next(e)
  })

  // The user touching the prompt box. Typing and deletion (a kill included) arrive as prompt.edit; prompt.change
  // carries every other write: history recall, a clear, a plugin's fill. Both run per edit, so they only record
  // the time and never make the chain wait for it.
  on('prompt.edit', async ($, e, next) => {
    boxTouched($, s)
    return next(e)
  })
  on('prompt.change', async ($, e, next) => {
    boxTouched($, s)
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e, next) => {
    void next
    try {
      return { text: statusText(s) }
    } catch (err) {
      return { text: `status unavailable: ${describeError(err)}` }
    }
  })

  on('session.end', async ($, e, next) => {
    try {
      endRun(s)
    } catch (err) {
      note($, s, `session.end error: ${describeError(err)}`)
    }
    return next(e)
  })
}
