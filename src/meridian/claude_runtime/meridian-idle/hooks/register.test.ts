import { expect, mock, test } from 'claude-code/testing'
import type { AgentInfo, On, SessionCompactResult } from 'claude-code'

type Host = {
  calls: string[][]
  draft: string
  agents: AgentInfo[]
  reply: Record<string, unknown | ((args: string[]) => unknown)>
  compactResult: () => SessionCompactResult
  compactCalls: number
}

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function host(): Host {
  return {
    calls: [],
    draft: '',
    agents: [],
    reply: {
      config: { enabled: true },
      arm: {
        stretch: 1,
        anchor: 1,
        push_at: START + 10_000,
        warn_at: START + 20_000,
        compact_at: START + 30_000,
      },
      return: { stretch_closed: true },
      fire: { decision: 'act', reason: 'guards-passed' },
      done: {},
      status: [],
    },
    compactResult: () => ({
      messages: [{ role: 'user', text: 'summary', toolUses: [] }],
      tokensBefore: 120_000,
      tokensAfter: 30_000,
    }),
    compactCalls: 0,
  }
}

function install(on: On, h: Host) {
  const clock = mock.clock(on, { now: START })
  mock.store(on)
  on('process.run', (_$, e) => {
    const args = e.argv.slice(2)
    h.calls.push([...args])
    const reply = h.reply[args[0] ?? '']
    const body = typeof reply === 'function' ? reply(args) : reply
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify(body) + '\n',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work/project' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000, tokens: 120_000 }, rateLimits: [] } }))
  on('prompt.read', () => ({ value: { text: h.draft, cursor: h.draft.length } }))
  on('agent.list', () => ({ value: h.agents }))
  on('config.list', () => ({ value: [{ key: 'autoCompact', label: 'Auto-compact', kind: 'boolean', value: true }] as never }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.compact', () => {
    h.compactCalls += 1
    return h.compactResult()
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('command.run', () => ({ text: '' }))
  return clock
}

function calls(h: Host, command: string): string[][] {
  return h.calls.filter(args => args[0] === command)
}

function flag(args: string[], name: string): string | undefined {
  const at = args.indexOf(name)
  return at < 0 ? undefined : args[at + 1]
}

function expectCliContract(h: Host): void {
  expect(h.calls.every(args => args.at(-1) === '--interactive')).toBe(true)
  for (const args of h.calls.filter(args => ['config', 'arm', 'return', 'fire', 'done'].includes(args[0] ?? ''))) {
    expect(flag(args, '--harness')).toBe('claude')
    if (args[0] !== 'config') expect(flag(args, '--session')).toBe('sess-1')
  }
}

test('print mode keeps the mod inert', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start({ cwd: '/work/project', surface: null, isInteractive: false })
  await $.turn.complete(MAIN_TURN)
  await $.prompt.submit({ text: 'hi', wait: false, origin: COMPOSER })
  await $.command.run({ command: 'compact', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
  await clock.advance(100_000)

  expect(h.calls).toEqual([])
})

test('a completed turn arms every returned deadline and reports a successful compaction', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(30_000)

  expect(calls(h, 'arm')).toEqual([
    [
      'arm', '--harness', 'claude', '--session', 'sess-1', '--cwd', '/work/project',
      '--assistant-text=done', '--interactive',
    ],
  ])
  expect(calls(h, 'fire').map(args => args[1])).toEqual(['push', 'warn', 'compact'])
  expect(calls(h, 'fire')[2]).toEqual([
    'fire', 'compact', '--harness', 'claude', '--session', 'sess-1',
    '--stretch', '1', '--anchor', '1', '--draft', 'no', '--agents-running', '0',
    '--context-tokens', '120000', '--interactive',
  ])
  expect(h.compactCalls).toBe(1)
  expect(calls(h, 'done')).toEqual([[
    'done', 'compact', '--harness', 'claude', '--session', 'sess-1',
    '--stretch', '1', '--result', 'ok', '--reason', '120k -> 30k tokens', '--interactive',
  ]])
  expectCliContract(h)
})

test('only composer input returns, and both prompts and slash commands clear timers', async ($, on) => {
  const h = host()
  h.reply.arm = () => ({
    stretch: 1,
    anchor: 1,
    push_at: Date.now() + 10_000,
  })
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  await $.prompt.submit({ text: 'injected', wait: false, origin: { kind: 'plugin', name: 'x' } })
  expect(calls(h, 'return')).toEqual([])

  await $.prompt.submit({ text: 'back', wait: false, origin: COMPOSER })
  await clock.advance(12_000)
  expect(calls(h, 'return')).toEqual([[
    'return', '--harness', 'claude', '--session', 'sess-1', '--user-prompt', '--interactive',
  ]])
  expect(calls(h, 'fire')).toEqual([])

  await $.turn.complete(MAIN_TURN)
  await clock.advance(2000)
  await $.command.run({ command: 'compact', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
  await clock.advance(12_000)
  expect(calls(h, 'return').length).toBe(2)
  expect(calls(h, 'fire')).toEqual([])
  expectCliContract(h)
})

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
  await clock.advance(30_000)

  expect(h.compactCalls).toBe(0)
  expect(h.calls.find(args => args[0] === 'done')?.filter(arg => arg !== '--interactive').slice(-4)).toEqual([
    '--result', 'vetoed', '--reason', 'draft',
  ])
})

test('reload recovery skips completed stages and an absorbed arm keeps its timers', async ($, on) => {
  const h = host()
  h.reply.status = [{
    harness: 'claude',
    session: 'sess-1',
    stretch: 4,
    anchor: 3,
    stretch_open: true,
    done: { push: 'sent' },
    schedule: { push_at: START + 10_000, warn_at: START + 20_000 },
  }]
  h.reply.arm = { stretch: 4, anchor: 3, reason: 'compact-window' }
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(20_000)

  expect(calls(h, 'status')).toEqual([['status', '--json', '--interactive']])
  expect(calls(h, 'arm')).toEqual([[
    'arm', '--harness', 'claude', '--session', 'sess-1', '--cwd', '/work/project',
    '--assistant-text=done', '--interactive',
  ]])
  expect(calls(h, 'fire').map(args => args[1])).toEqual(['warn'])
  expect(calls(h, 'fire')[0]).toEqual([
    'fire', 'warn', '--harness', 'claude', '--session', 'sess-1',
    '--stretch', '4', '--anchor', '3', '--interactive',
  ])
  expectCliContract(h)
})

test('arm excerpts use equals-form argv and only apply a safety length cap', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  const answer = `- ${'answer '.repeat(700)}`

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.prompt.submit({
    text: '  can u test this?\n',
    wait: false,
    origin: COMPOSER,
  })
  await $.turn.complete({ ...MAIN_TURN, answer })
  await clock.advance(0)

  const arm = calls(h, 'arm')[0]
  expect(arm).toContain('--user-text=  can u test this?\n')
  const assistant = arm.find(arg => arg.startsWith('--assistant-text='))
  expect(assistant?.slice('--assistant-text='.length)).toBe(answer.slice(0, 4000))
  expect(assistant).toContain('--assistant-text=- ')
})
