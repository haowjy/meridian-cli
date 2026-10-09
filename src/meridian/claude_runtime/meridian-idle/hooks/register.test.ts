import { expect, mock, test } from 'claude-code/testing'
import type { AgentInfo, On, SessionCompactResult } from 'claude-code'

// A fake `meridian idle` and a fake host beneath the plugin. Every call the
// mod makes lands in `calls`, so a test reads the contract off argv.

type Host = {
  /** argv after `meridian idle`, in call order */
  calls: string[][]
  draft: string
  agents: AgentInfo[]
  tokens: number | undefined
  autoCompact: boolean
  /** replies by subcommand; a function sees the full argv */
  reply: Record<string, unknown | ((args: string[]) => unknown)>
  compactResult: () => SessionCompactResult
  compactCalls: number
  registered: string[]
}

const START = 1_000_000

function agent(id: string, status: AgentInfo['status']): AgentInfo {
  return { id, description: `task ${id}`, type: 'general-purpose', status }
}

function host(): Host {
  return {
    calls: [],
    draft: '',
    agents: [],
    tokens: 120_000,
    autoCompact: true,
    reply: {
      config: { enabled: true, push_seconds: 60, warn_minutes: 15, compact_minutes: 5, compact: true, min_compact_tokens: 40000 },
      arm: { stretch: 1, anchor: 1, push_at: START + 60_000, warn_at: START + 900_000, compact_at: START + 1_500_000 },
      return: { stretch_closed: true },
      fire: { decision: 'act', reason: 'guards-passed' },
      done: {},
      status: [],
    },
    compactResult: () => ({ messages: [{ role: 'user', text: 'summary', toolUses: [] }], tokensBefore: 120_000, tokensAfter: 30_000 }),
    compactCalls: 0,
    registered: [],
  }
}

function install(on: On, h: Host) {
  const clock = mock.clock(on, { now: START })
  mock.store(on)
  on('process.run', (_$, e) => {
    const args = e.argv.slice(2)
    h.calls.push([...args])
    const sub = args[0] ?? ''
    const reply = h.reply[sub]
    const body = typeof reply === 'function' ? reply(args) : reply
    return { value: { exitCode: 0, stdout: JSON.stringify(body) + '\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work/project' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000, ...(h.tokens === undefined ? {} : { tokens: h.tokens }) }, rateLimits: [] } }))
  on('prompt.read', () => ({ value: { text: h.draft, cursor: h.draft.length } }))
  on('agent.list', () => ({ value: h.agents }))
  on('config.list', () => ({ value: [{ key: 'autoCompact', label: 'Auto-compact', kind: 'boolean', value: h.autoCompact }] as never }))
  on('command.register', (_$, e) => {
    h.registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('session.compact', () => {
    h.compactCalls += 1
    return h.compactResult()
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.end', () => ({ sessionId: 'sess-1' }))
  on('turn.complete', () => ({ text: '' }))
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('command.run', () => ({ text: '' }))
  return clock
}

const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function names(h: Host): string[] {
  return h.calls.map(args => args.slice(0, 2).join(' ').trim())
}

function flag(args: string[] | undefined, name: string): string | undefined {
  const at = args?.indexOf(name) ?? -1
  return at < 0 ? undefined : args?.[at + 1]
}

function fireCalls(h: Host, stage: string): string[][] {
  return h.calls.filter(args => args[0] === 'fire' && args[1] === stage)
}

test('session.start in print mode keeps the mod inert: no config call, no arm, no commands', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start({ cwd: '/work/project', surface: null, isInteractive: false })
  await $.turn.complete(MAIN_TURN)
  await $.prompt.submit({ text: 'hi', wait: false, origin: COMPOSER })
  await clock.advance(10_000)
  expect(h.calls).toEqual([])
  expect(h.registered).toEqual([])
})

test('a disabled config (role gate) keeps the mod inert and arms nothing', async ($, on) => {
  const h = host()
  h.reply.config = { enabled: false, reason: 'role', push_seconds: 60, warn_minutes: 15, compact_minutes: 5, compact: true, min_compact_tokens: 40000 }
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  expect(names(h)).toEqual(['config --harness'])
  expect(h.calls[0]).toEqual(['config', '--harness', 'claude', '--interactive'])
})

test('session.start asks core for the config with --interactive and registers /meridian-idle', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  expect(h.calls[0]).toEqual(['config', '--harness', 'claude', '--interactive'])
  expect(h.registered).toEqual(['meridian-idle'])
})

test('a main-loop turn.complete arms and sets one timer per returned stage; a subagent turn does not arm', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete({ ...MAIN_TURN, agentId: 'agent-1' })
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'arm')).toEqual([])

  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  const arms = h.calls.filter(args => args[0] === 'arm')
  expect(arms).toEqual([['arm', '--harness', 'claude', '--session', 'sess-1', '--cwd', '/work/project']])

  // push is due at START + 60 s, absolute: fires once the clock crosses it
  await clock.advance(50_000)
  expect(fireCalls(h, 'push')).toEqual([])
  await clock.advance(10_000)
  expect(fireCalls(h, 'push')).toEqual([['fire', 'push', '--harness', 'claude', '--session', 'sess-1', '--stretch', '1', '--anchor', '1']])
})

test('arm is skipped while a subagent is still running', async ($, on) => {
  const h = host()
  h.agents = [agent('a1', 'running')]
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'arm')).toEqual([])
  h.agents = [agent('a1', 'completed')]
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'arm').length).toBe(1)
})

test('a composer prompt cancels the timers and calls return --user-prompt; injected origins do not', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)

  for (const kind of ['task-notification', 'peer', 'plugin', 'sdk'] as const) {
    const origin = kind === 'plugin' ? { kind, name: 'x' } : { kind }
    await $.prompt.submit({ text: 'injected', wait: false, origin } as never)
  }
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'return')).toEqual([])

  await $.prompt.submit({ text: 'I am back', wait: false, origin: COMPOSER })
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'return')).toEqual([['return', '--harness', 'claude', '--session', 'sess-1', '--user-prompt']])

  await clock.advance(3_600_000)
  expect(h.calls.filter(args => args[0] === 'fire')).toEqual([])
})

test('a typed slash command is a return; /meridian-idle itself is not and prints the timers', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)

  const shown = await $.command.run({ command: 'meridian-idle', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
  expect(shown.text).toContain('stretch 1, anchor 1')
  expect(shown.text).toContain('push')
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'return')).toEqual([])

  await $.command.run({ command: 'compact', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
  await clock.advance(2000)
  expect(h.calls.filter(args => args[0] === 'return').length).toBe(1)
  await clock.advance(3_600_000)
  expect(h.calls.filter(args => args[0] === 'fire')).toEqual([])
})

test('compact: facts are mapped to flags, act triggers $.session.compact from the timer, done --result ok', async ($, on) => {
  const h = host()
  h.agents = [agent('a1', 'completed')]
  h.autoCompact = false
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(1_600_000)
  const [fire] = fireCalls(h, 'compact')
  expect(fire).toEqual([
    'fire', 'compact', '--harness', 'claude', '--session', 'sess-1', '--stretch', '1', '--anchor', '1',
    '--draft', 'no', '--agents-running', '0', '--context-tokens', '120000', '--harness-autocompact-off',
  ])
  expect(h.compactCalls).toBe(1)
  const done = h.calls.find(args => args[0] === 'done')
  expect(done).toEqual(['done', 'compact', '--harness', 'claude', '--session', 'sess-1', '--stretch', '1', '--result', 'ok', '--reason', '120k -> 30k tokens'])
  // push and warn went through fire too, once each, with no facts
  expect(fireCalls(h, 'push').length).toBe(1)
  expect(fireCalls(h, 'warn').length).toBe(1)
  expect(flag(fireCalls(h, 'push')[0], '--draft')).toBeUndefined()
})

test('compact facts: a draft is yes, running agents are counted, unknown tokens are omitted', async ($, on) => {
  const h = host()
  h.draft = 'half typed'
  h.tokens = undefined
  h.agents = [agent('a1', 'running'), agent('a2', 'completed'), agent('a3', 'running')]
  h.reply.fire = (args: string[]) => (args[1] === 'compact' ? { decision: 'skip', reason: 'draft' } : { decision: 'act', reason: 'guards-passed' })
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  // arm gate skips while agents run, so let them finish for the arm and bring them back for the fire
  const running = h.agents
  h.agents = []
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  h.agents = running
  await clock.advance(1_600_000)
  const [fire] = fireCalls(h, 'compact')
  expect(flag(fire, '--draft')).toBe('yes')
  expect(flag(fire, '--agents-running')).toBe('2')
  expect(fire?.includes('--context-tokens')).toBe(false)
  expect(fire?.includes('--harness-autocompact-off')).toBe(false)
  // a skip means: do nothing
  expect(h.compactCalls).toBe(0)
  expect(h.calls.filter(args => args[0] === 'done')).toEqual([])
})

function doneMapping(outcome: 'veto' | 'reject', expected: string[]) {
  test(`done maps a ${outcome === 'veto' ? 'vetoed compaction to --result vetoed' : 'rejected compaction to --result failed'} with the reason`, async ($, on) => {
    const h = host()
    h.compactResult = () => {
      if (outcome === 'reject') throw new Error('compact exploded')
      return { skip: 'blocked by a hook' }
    }
    const clock = install(on, h)
    await $.session.start(TUI)
    await clock.advance(2000)
    await $.turn.complete(MAIN_TURN)
    await clock.advance(1_600_000)
    expect(h.compactCalls).toBe(1)
    const done = h.calls.find(args => args[0] === 'done') ?? []
    if (outcome === 'veto') {
      expect(done.slice(-4)).toEqual(expected)
    } else {
      // the kit words a rejection its own way: check the result flag and that a reason travels
      expect(done.slice(-4, -2)).toEqual(expected.slice(0, 2))
      expect(done.at(-2)).toBe('--reason')
      expect(done.at(-1)).not.toBe('')
    }
  })
}

doneMapping('veto', ['--result', 'vetoed', '--reason', 'blocked by a hook'])
doneMapping('reject', ['--result', 'failed', '--reason', 'compact exploded'])

test('a draft typed after core said act vetoes the compaction instead of compacting over it', async ($, on) => {
  const h = host()
  h.reply.fire = (args: string[]) => {
    if (args[1] === 'compact') h.draft = 'typed while fire ran'
    return { decision: 'act', reason: 'guards-passed' }
  }
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(1_600_000)
  expect(h.compactCalls).toBe(0)
  expect(h.calls.find(args => args[0] === 'done')?.slice(-4)).toEqual(['--result', 'vetoed', '--reason', 'draft'])
})

test('a re-arm with a new anchor replaces the old timers; the same stretch and anchor keeps them', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)

  // second turn, anchor 2, pushes the schedule later
  h.reply.arm = { stretch: 1, anchor: 2, push_at: START + 200_000, warn_at: START + 950_000 }
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  await clock.advance(100_000) // past the old push_at (START + 60 s)
  expect(fireCalls(h, 'push')).toEqual([])
  await clock.advance(100_000) // past the new one
  expect(fireCalls(h, 'push').length).toBe(1)
  expect(flag(fireCalls(h, 'push')[0], '--anchor')).toBe('2')

  // a window-absorbed arm: same stretch and anchor, no deadlines, a reason -> keep timers
  h.reply.arm = { stretch: 1, anchor: 2, reason: 'compact-window' }
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  await clock.advance(1_000_000)
  expect(fireCalls(h, 'warn').length).toBe(1)
})

test('after a reload the mod rebuilds timers from `idle status --json`, skipping stages already done', async ($, on) => {
  const h = host()
  h.reply.status = [
    { harness: 'claude', session: 'other-session', stretch: 9, anchor: 9, stretch_open: true, done: {}, schedule: { push_at: START + 1000 } },
    {
      harness: 'claude', session: 'sess-1', stretch: 4, anchor: 3, stretch_open: true,
      done: { push: 'sent' },
      schedule: { push_at: START + 1000, warn_at: START + 30_000, compact_at: START + 60_000 },
    },
  ]
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  expect(h.calls.map(args => args[0])).toEqual(['config', 'status'])
  expect(h.calls[1]).toEqual(['status', '--json'])
  await clock.advance(40_000)
  expect(fireCalls(h, 'push')).toEqual([])
  expect(fireCalls(h, 'warn')).toEqual([['fire', 'warn', '--harness', 'claude', '--session', 'sess-1', '--stretch', '4', '--anchor', '3']])
  await clock.advance(30_000)
  expect(fireCalls(h, 'compact').length).toBe(1)
})

test('a failing meridian never throws into the hooks and arms nothing', async ($, on) => {
  const h = host()
  h.reply.arm = { error: 'boom' }
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(1_700_000)
  expect(h.calls.filter(args => args[0] === 'fire')).toEqual([])
})
