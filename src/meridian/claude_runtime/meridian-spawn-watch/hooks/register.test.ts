import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderSurface } from 'claude-code'

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const COMPOSER = { kind: 'composer' } as const
const IDLE_DELAY = 2_000
const QUIET = 10_000

type Exit = { stdout?: string; stderr?: string; code?: number }

/** One `meridian spawn collect` the mod started. `finish` is the test playing the process. */
type Collector = { argv: string[]; killed: boolean; finish: (exit: Exit) => void }

type Host = {
  collectors: Collector[]
  /** Called with each collector as it starts. */
  onCollector: ((c: Collector) => void) | undefined
  /** `meridian spawn ack` argv tails. */
  acks: string[][]
  ackExit: number
  ackThrows: boolean
  /** While set, `meridian spawn ack` is still running; the test releases it. */
  ackGate: Promise<void> | undefined
  /** Texts the mod passed to `$.prompt.submit`. */
  submits: string[]
  submitDrop: string | undefined
  submitThrows: boolean
  /** Runs inside the submit, before it resolves: the engine starting the prompt's turn. */
  duringSubmit: ((text: string) => Promise<unknown>) | undefined
  /** What `$.prompt.read()` answers. */
  box: string
  /** `$.process.spawn` itself fails (meridian not on PATH). */
  spawnFails: boolean
  surfaces: RenderSurface[]
}

function host(): Host {
  return {
    collectors: [],
    onCollector: undefined,
    acks: [],
    ackExit: 0,
    ackThrows: false,
    ackGate: undefined,
    submits: [],
    submitDrop: undefined,
    submitThrows: false,
    duringSubmit: undefined,
    box: '',
    spawnFails: false,
    surfaces: ['terminal'],
  }
}

function install(on: On, h: Host, options: { store?: boolean } = {}) {
  const clock = mock.clock(on, { now: START })
  if (options.store !== false) mock.store(on)
  on('process.spawn', async function* (_$, e, next) {
    const c: Collector = { argv: [...e.argv], killed: false, finish: () => undefined }
    const ended = new Promise<Exit | 'killed'>(resolve => {
      c.finish = resolve
      next.signal.addEventListener('abort', () => {
        c.killed = true
        resolve('killed')
      }, { once: true })
    })
    h.collectors.push(c)
    h.onCollector?.(c)
    if (h.spawnFails) throw new Error('spawn meridian ENOENT')
    const exit = await ended
    if (exit === 'killed') return { value: { code: null, signal: 'SIGTERM' } }
    if (exit.stderr !== undefined) yield { stream: 'stderr', text: exit.stderr }
    if (exit.stdout !== undefined) yield { stream: 'stdout', text: exit.stdout }
    return { value: { code: exit.code ?? 0, signal: null } }
  })
  on('process.run', async (_$, e) => {
    h.acks.push(e.argv.slice(3))
    await h.ackGate
    if (h.ackThrows) throw new Error('meridian vanished')
    return {
      value: { exitCode: h.ackExit, stdout: '{"acked": []}\n', stderr: h.ackExit === 0 ? '' : 'lock busy', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  on('prompt.submit', async (_$, e) => {
    h.submits.push(e.text)
    if (h.submitThrows) throw new Error('engine refused')
    await h.duringSubmit?.(e.text)
    return h.submitDrop === undefined ? { text: e.text } : { drop: h.submitDrop }
  })
  on('prompt.read', () => ({ value: { text: h.box, cursor: h.box.length } }))
  on('prompt.edit', (_$, e) => ({ text: e.text, cursor: e.cursor }))
  on('prompt.change', (_$, e) => ({ text: e.text }))
  on('session.surfaces', () => ({ value: h.surfaces }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.end', () => ({ sessionId: 'sess-1' }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  return clock
}

let turnCount = 0

function mainTurn(reason: 'answer' | 'aborted' | 'error' | 'refusal' = 'answer') {
  turnCount += 1
  const base = { answer: 'done', durationMs: 5, isAborted: reason === 'aborted', turnId: `t${turnCount}` }
  return reason === 'refusal'
    ? { ...base, reason, refusal: { category: null, explanation: null } }
    : { ...base, reason }
}

const delivery = (ids: string[], text = 'p12 finished: ok') => ({ stdout: JSON.stringify({ kind: 'delivery', ids, text }) })
const EMPTY: Exit = { stdout: JSON.stringify({ kind: 'empty', ids: [], text: '' }) }

/** Resolves with the next collector the mod starts. */
function nextCollector(h: Host): Promise<Collector> {
  return new Promise(resolve => {
    h.onCollector = resolve
  })
}

/**
 * The main loop goes idle and the 2 s delay passes; resolves with the collector that started.
 *
 * The clock's advance does not settle until the collector's stream ends (it gives up after 1 s of real time),
 * so it is left running here and finishes when the test ends or kills the collector.
 */
async function idle($: Engine, clock: ReturnType<typeof mock.clock>, h: Host): Promise<Collector> {
  const started = nextCollector(h)
  await $.turn.complete(mainTurn())
  void clock.advance(IDLE_DELAY)
  return started
}

/** A turn runs and ends with `reason`, and the 2 s delay passes with no collector expected. */
async function quiet($: Engine, clock: ReturnType<typeof mock.clock>, reason: Parameters<typeof mainTurn>[0]) {
  await $.turn.start({ text: 'user prompt', turnId: 'q1' })
  await $.turn.complete(mainTurn(reason))
  await clock.advance(IDLE_DELAY)
}

function last<T>(items: T[]): T {
  const item = items.at(-1)
  if (item === undefined) throw new Error('expected at least one item')
  return item
}

function exclude(c: Collector): string | undefined {
  const at = c.argv.indexOf('--exclude')
  return at < 0 ? undefined : c.argv[at + 1]
}

function nonceOf(submitted: string): string {
  const match = /delivery (\S+)\]/.exec(submitted)
  if (match?.[1] === undefined) throw new Error(`no nonce in ${submitted}`)
  return match[1]
}

/** Runs one delivery up to the submit: idle, the collector returns `ids`, the quiet box lets it through. */
async function deliverOnce($: Engine, clock: ReturnType<typeof mock.clock>, h: Host, ids: string[]) {
  const collector = await idle($, clock, h)
  collector.finish(delivery(ids))
  await clock.settle()
}

/** The engine's own events have no call on `$` in the typings; a test raises them by name. */
async function raise($: Engine, event: 'edit' | 'change', input: unknown) {
  await ($.prompt as unknown as Record<string, (e: unknown) => Promise<unknown>>)[event]?.(input)
}

const typed = (text: string) => ({ origin: { kind: 'composer' }, text, cursor: text.length, start: 0, end: 0, inputText: 'x' })
const changed = (text: string) => ({ text, cursor: text.length, origin: { kind: 'engine' } })

function statusCommand($: Engine) {
  return $.command.run({ command: 'meridian-spawn-watch', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
}

test('print mode keeps the mod inert', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start({ cwd: '/work/project', surface: null, isInteractive: false })
  await $.turn.complete(mainTurn())
  await clock.advance(100_000)

  expect(h.collectors).toEqual([])
  expect(h.submits).toEqual([])
  expect(h.acks).toEqual([])
})

test('a session that starts idle (fresh, resumed, reloaded) collects 2 s after session.start and delivers what is owed, with no turn', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI) // returns at once: the run is not part of the hook
  await clock.advance(IDLE_DELAY - 1)
  expect(h.collectors).toHaveLength(0)

  const started = nextCollector(h)
  void clock.advance(1)
  expect((await started).argv).toEqual(['meridian', 'spawn', 'collect'])

  last(h.collectors).finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(1)
  const sent = last(h.submits)
  expect(sent).toContain('p12 finished: ok')
  expect(h.acks).toEqual([])

  await $.turn.start({ text: sent, turnId: 'd1' })
  await clock.settle()
  expect(h.acks).toEqual([['p12']])
})

test('the first prompt ends the run session.start began, whether it is waiting or collecting', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(IDLE_DELAY - 500)
  await $.turn.start({ text: 'first prompt', turnId: 'u1' })
  await clock.advance(60_000)
  expect(h.collectors).toHaveLength(0)

  // session.start again (a reload) while the first run's work is done: a new run replaces whatever was in flight.
  const started = nextCollector(h)
  await $.session.start(TUI)
  void clock.advance(IDLE_DELAY)
  const running = await started
  await $.turn.start({ text: 'second prompt', turnId: 'u2' })
  expect(running.killed).toBe(true)

  running.finish(delivery(['p12']))
  await clock.advance(60_000)
  expect(h.submits).toEqual([])
})

test('the collector starts 2 s after a main-loop answer and an empty verdict does nothing', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await $.turn.complete(mainTurn())
  await clock.advance(IDLE_DELAY - 1)
  expect(h.collectors).toHaveLength(0)

  const started = nextCollector(h)
  void clock.advance(1)
  expect((await started).argv).toEqual(['meridian', 'spawn', 'collect'])

  last(h.collectors).finish(EMPTY)
  await clock.advance(60_000)
  expect(h.submits).toEqual([])
  expect(h.acks).toEqual([])
})

test('no collector after an aborted, errored or refused turn, nor after a subagent turn', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await quiet($, clock, 'aborted')
  await quiet($, clock, 'error')
  await quiet($, clock, 'refusal')
  await $.turn.complete({ ...mainTurn(), agentId: 'agent-1' })
  await clock.advance(60_000)
  expect(h.collectors).toHaveLength(0)

  await idle($, clock, h)
  expect(h.collectors).toHaveLength(1)
})

test('a main-loop turn.start ends the pending delay, and a running collector is killed with its late reply ignored', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await $.turn.complete(mainTurn())
  await clock.advance(IDLE_DELAY - 500)
  await $.turn.start({ text: 'user prompt', turnId: 'u1' })
  await clock.advance(IDLE_DELAY)
  expect(h.collectors).toHaveLength(0)

  const running = await idle($, clock, h)
  expect(running.killed).toBe(false)
  await $.turn.start({ text: 'another prompt', turnId: 'u2' })
  expect(running.killed).toBe(true)

  running.finish(delivery(['p12']))
  await clock.advance(60_000)
  expect(h.submits).toEqual([])
})

test('subagent turns neither cancel the delay nor kill the collector', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await $.turn.complete(mainTurn())
  await clock.advance(IDLE_DELAY - 500)
  await $.turn.start({ text: 'review the diff', turnId: 'a1', agentId: 'agent-1' })
  const started = nextCollector(h)
  void clock.advance(500)
  await started
  expect(h.collectors).toHaveLength(1)

  await $.turn.start({ text: 'compact', turnId: 'a2', agentId: 'fork-1' })
  await $.turn.complete({ ...mainTurn(), agentId: 'agent-1' })
  expect(last(h.collectors).killed).toBe(false)
  expect(h.collectors).toHaveLength(1)

  last(h.collectors).finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(1)
})

test('a delivery is acked only on the main-loop turn that carries its nonce', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await deliverOnce($, clock, h, ['p12', 'p13'])

  expect(h.submits).toHaveLength(1)
  const sent = last(h.submits)
  expect(sent).toContain('p12 finished: ok')
  expect(sent).toContain(nonceOf(sent))

  await $.turn.start({ text: 'unrelated user prompt', turnId: 'u1' })
  await $.turn.start({ text: sent, turnId: 's1', agentId: 'agent-1' }) // a subagent echoing the text is not the delivery
  await clock.settle()
  expect(h.acks).toEqual([])

  await $.turn.start({ text: `The meridian-spawn-watch plugin sent a message:\n${sent}`, turnId: 'd1' })
  await clock.settle()
  expect(h.acks).toEqual([['p12', 'p13']])

  // Acked: the ids are released, so the next collector asks for everything owed.
  const next = await idle($, clock, h)
  expect(exclude(next)).toBeUndefined()
})

test('a collector started while the ack is still running excludes the claimed ids', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  let release: () => void = () => undefined
  h.ackGate = new Promise<void>(resolve => {
    release = resolve
  })

  await $.session.start(TUI)
  await deliverOnce($, clock, h, ['p12', 'p13'])
  await $.turn.start({ text: last(h.submits), turnId: 'd1' })

  // The delivered turn ends and the lead idles before the slow ack returns.
  const second = await idle($, clock, h)
  expect(exclude(second)).toBe('p12,p13')
  second.finish(EMPTY)

  release()
  await clock.settle()
  const third = await idle($, clock, h)
  expect(exclude(third)).toBeUndefined()
})

test('the delivery turn can start before submit resolves and is still acked once', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  h.duringSubmit = text => $.turn.start({ text, turnId: 'd1' })

  await $.session.start(TUI)
  await deliverOnce($, clock, h, ['p20'])
  await clock.settle()

  expect(h.acks).toEqual([['p20']])
})

test('a dropped or failed submit is never acked and releases its claim', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  h.submitDrop = 'a hook refused it'
  await deliverOnce($, clock, h, ['p12'])
  expect(h.submits).toHaveLength(1)

  h.submitDrop = undefined
  h.submitThrows = true
  const second = await idle($, clock, h)
  expect(exclude(second)).toBeUndefined()
  second.finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(2)

  const third = await idle($, clock, h)
  expect(exclude(third)).toBeUndefined()
  expect(h.acks).toEqual([])
})

test('a queued delivery that never starts its turn is never acked, and the next idle releases and redelivers it', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await deliverOnce($, clock, h, ['p12'])
  const first = last(h.submits)

  // Esc cleared the queue: no turn ever carried the nonce. A user turn runs instead, then the lead idles again.
  await $.turn.start({ text: 'typed instead', turnId: 'u1' })
  const second = await idle($, clock, h)
  expect(exclude(second)).toBeUndefined()
  expect(h.acks).toEqual([])

  second.finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(2)
  expect(nonceOf(last(h.submits))).not.toBe(nonceOf(first))
})

test('an ack that fails is logged and keeps its ids excluded instead of redelivering at every idle', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await deliverOnce($, clock, h, ['p12'])
  h.ackExit = 1
  await $.turn.start({ text: last(h.submits), turnId: 'd1' })
  await clock.settle()
  expect(h.acks).toEqual([['p12']])

  const second = await idle($, clock, h)
  expect(exclude(second)).toBe('p12')
  second.finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(1)

  const status = await statusCommand($)
  expect(status.text).toContain('ack p12 failed (exit 1): lock busy')
  expect(status.text).toContain('(unacked)')
})

test('a draft in the prompt box holds the delivery until it has been empty and untouched for 10 s', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  h.box = 'half a thought'
  await deliverOnce($, clock, h, ['p12'])
  await clock.advance(60_000)
  expect(h.submits).toEqual([])

  // The user deletes the draft: a kill is a prompt.edit, which the box only reflects a moment later.
  h.box = ''
  await raise($, 'edit', typed(''))
  await clock.advance(QUIET - 2_000)
  expect(h.submits).toEqual([])

  await clock.advance(2_000)
  expect(h.submits).toHaveLength(1)
})

test('any later prompt.change (history recall, a clear, a plugin fill) restarts the 10 s of quiet', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await raise($, 'change', changed(''))
  await deliverOnce($, clock, h, ['p12'])
  await clock.advance(QUIET - 3_000)
  expect(h.submits).toEqual([])

  await raise($, 'change', changed(''))
  await clock.advance(QUIET - 1_000)
  expect(h.submits).toEqual([])

  await clock.advance(1_000)
  expect(h.submits).toHaveLength(1)
})

test('the user sending their draft ends the hold: nothing is submitted and the claim is released', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  h.box = 'my own question'
  await deliverOnce($, clock, h, ['p12'])
  await clock.advance(5_000)

  await $.turn.start({ text: 'my own question', turnId: 'u1' })
  h.box = ''
  await clock.advance(60_000)
  expect(h.submits).toEqual([])
  expect(h.acks).toEqual([])

  const next = await idle($, clock, h)
  expect(exclude(next)).toBeUndefined()
})

test('a collector that fails or answers badly submits nothing and the next idle tries again', { timeoutMs: 10_000 }, async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  const bad: Exit[] = [
    { code: 3, stderr: 'index busy' },
    { stdout: 'not json' },
    { stdout: JSON.stringify({ kind: 'checkpoint', ids: [], text: '' }) },
    { stdout: JSON.stringify({ kind: 'delivery', ids: [], text: 'x' }) },
    { stdout: JSON.stringify({ kind: 'delivery', ids: ['--help'], text: 'x' }) },
    { stdout: JSON.stringify({ kind: 'delivery', ids: ['p1'] }) },
  ]
  for (const exit of bad) {
    const collector = await idle($, clock, h)
    collector.finish(exit)
    await clock.settle()
  }
  expect(h.collectors).toHaveLength(bad.length)
  expect(h.submits).toEqual([])

  const status = await statusCommand($)
  expect(status.text).toContain('collect exited 3: index busy')
  expect(status.text).toContain('unparseable output')

  const good = await idle($, clock, h)
  good.finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(1)
})

test('a reload mid-session (no session.start) starts clean from the next turn', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await deliverOnce($, clock, h, ['p12'])
  expect(h.submits).toHaveLength(1)
  expect(exclude(h.collectors[0] as Collector)).toBeUndefined()
})

test('a reload into a session with no surface stays inert', async ($, on) => {
  const h = host()
  h.surfaces = []
  const clock = install(on, h)

  await $.turn.complete(mainTurn())
  await clock.advance(100_000)
  expect(h.collectors).toEqual([])
})

test('hooks never throw: a collector that cannot start, a store that fails, an ack that errors', async ($, on) => {
  const h = host()
  const clock = install(on, h, { store: false })
  on('store.set', () => {
    throw new Error('store full')
  })
  on('store.get', () => ({ value: undefined }))

  await $.session.start(TUI)
  h.spawnFails = true
  await $.turn.complete(mainTurn())
  await clock.advance(IDLE_DELAY)
  expect(h.collectors).toHaveLength(1)

  h.spawnFails = false
  const second = await idle($, clock, h)
  second.finish(delivery(['p12']))
  await clock.settle()
  expect(h.submits).toHaveLength(1)

  h.ackThrows = true
  await $.turn.start({ text: last(h.submits), turnId: 'd1' })
  await clock.settle()
  expect(h.acks).toEqual([['p12']])
  expect((await statusCommand($)).text).toContain('ack p12 failed')
})

test('/meridian-spawn-watch reports state without counting as a turn', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  expect((await statusCommand($)).text).toContain('idle run: delay')

  // Asking did not end the run session.start began: its collector still starts.
  const started = nextCollector(h)
  void clock.advance(IDLE_DELAY)
  const first = await started
  expect((await statusCommand($)).text).toContain('idle run: collecting')
  expect(first.killed).toBe(false)
  first.finish(EMPTY)
  await clock.settle()
  expect((await statusCommand($)).text).toContain('idle run: none')

  const collector = await idle($, clock, h)
  collector.finish(delivery(['p12', 'p13']))
  await clock.settle()
  const status = (await statusCommand($)).text
  expect(status).toContain('last collect: delivery p12,p13')
  expect(status).toContain('(queued)')
})
