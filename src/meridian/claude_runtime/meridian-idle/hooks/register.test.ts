import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

type Host = {
  calls: string[][]
  reply: Record<string, unknown | ((args: string[]) => unknown)>
}

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function host(): Host {
  return {
    calls: [],
    reply: {
      config: { enabled: true },
      arm: {
        stretch: 1,
        anchor: 1,
        push_at: START + 10_000,
        warn_at: START + 20_000,
      },
      return: { stretch_closed: true },
      fire: { decision: 'act', reason: 'guards-passed' },
      status: [],
    },
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
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  on('classic.Stop', () => ({}))
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
  for (const args of h.calls.filter(args => ['config', 'arm', 'return', 'fire'].includes(args[0] ?? ''))) {
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

test('a completed turn arms every returned deadline and fires each at its time', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(20_000)

  expect(calls(h, 'arm')).toEqual([
    ['arm', '--harness', 'claude', '--session', 'sess-1', '--assistant-text=done', '--interactive'],
  ])
  expect(calls(h, 'fire')).toEqual([
    ['fire', 'push', '--harness', 'claude', '--session', 'sess-1', '--stretch', '1', '--anchor', '1', '--interactive'],
    ['fire', 'warn', '--harness', 'claude', '--session', 'sess-1', '--stretch', '1', '--anchor', '1', '--interactive'],
  ])
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
  h.reply.arm = { stretch: 4, anchor: 3, reason: 'duplicate-turn' }
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(20_000)

  expect(calls(h, 'status')).toEqual([['status', '--json', '--interactive']])
  expect(calls(h, 'arm')).toEqual([
    ['arm', '--harness', 'claude', '--session', 'sess-1', '--assistant-text=done', '--interactive'],
  ])
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

test('arm forwards the work the main loop left running, from its Stop list', async ($, on) => {
  const h = host()
  h.reply.arm = { stretch: 1, anchor: 1, warn_at: START + 20_000, push_held: '1 agent(s) running' }
  const clock = install(on, h)
  const longCommand = `meridian spawn wait p3 --timeout 60 ${'large inline prompt '.repeat(100)}`
  const continuedCommand = `${'x'.repeat(508)}\\${'tail'.repeat(10)}`

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.start({ turnId: 't1' } as never)
  await $.classic.Stop({
    stop_hook_active: false,
    background_tasks: [
      { id: 'a', type: 'subagent', status: 'running', description: 'review' },
      { id: 'b', type: 'shell', status: 'running', description: 'wait', command: 'meridian spawn wait p3' },
      { id: 'c', type: 'monitor', status: 'running', description: 'logs' },
      { id: 'd', type: 'workflow', status: 'running', description: 'plan' },
      { id: 'e', type: 'shell', status: 'running', description: 'large', command: longCommand },
      { id: 'f', type: 'shell', status: 'running', description: 'continued', command: continuedCommand },
      ...Array.from({ length: 20 }, (_, index) => ({
        id: `extra-${index}`,
        type: 'monitor' as const,
        status: 'running' as const,
        description: 'extra',
      })),
    ],
    session_crons: [],
  } as never)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(0)

  const arm = calls(h, 'arm')[0]
  const tasksArg = arm.find(arg => arg.startsWith('--background-tasks='))
  const tasks = JSON.parse(tasksArg?.slice('--background-tasks='.length) ?? '') as Array<Record<string, string>>
  expect(arm.filter(arg => arg.startsWith('--background-tasks='))).toHaveLength(1)
  expect(tasks).toHaveLength(16)
  expect(tasks.slice(0, 4)).toEqual([
    { kind: 'agent' },
    { kind: 'shell', command: 'meridian spawn wait p3' },
    { kind: 'watcher' },
    { kind: 'agent' },
  ])
  expect(tasks[4]).toEqual({ kind: 'other' })
  expect(tasks[5]).toEqual({ kind: 'other' })
  expect(arm).toContain('--assistant-text=done')
  expectCliContract(h)
})
