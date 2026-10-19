import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

type Host = {
  calls: string[][]
  reply: Record<string, unknown | ((args: string[]) => unknown | Promise<unknown>)>
  statuses: (string | undefined)[]
}

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function host(): Host {
  return {
    calls: [],
    statuses: [],
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
      'child-status': '',
    },
  }
}

function install(on: On, h: Host) {
  const clock = mock.clock(on, { now: START })
  mock.store(on)
  on('process.run', async (_$, e) => {
    const args = e.argv.slice(2)
    h.calls.push([...args])
    const reply = h.reply[args[0] ?? '']
    const body = typeof reply === 'function' ? await reply(args) : reply
    const stdout = args[0] === 'child-status' ? String(body ?? '') : JSON.stringify(body)
    return {
      value: {
        exitCode: 0,
        stdout: stdout + '\n',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('ui.status', (_$, e) => {
    h.statuses.push(e.text)
    return { value: undefined }
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  on('classic.Stop', () => ({}))
  on('classic.Notification', () => ({}))
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('command.run', () => ({ text: '' }))
  on('tool.call', () => ({ result: 'ok' }))
  on('session.end', () => ({ sessionId: 'sess-1' }))
  return clock
}

function calls(h: Host, command: string): string[][] {
  return h.calls.filter(args => args[0] === command)
}

function flag(args: string[], name: string): string | undefined {
  const at = args.indexOf(name)
  return at < 0 ? undefined : args[at + 1]
}

function deferred<T>(): { promise: Promise<T>, resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
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

test('only permission prompts are forwarded and a failing CLI does not fail the hook', async ($, on) => {
  const h = host()
  const clock = install(on, h)
  const message = 'Claude needs your permission to use Bash'

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.classic.Notification({ message, notification_type: 'permission_prompt' } as never)
  await clock.advance(0)

  expect(calls(h, 'needs-permission')).toEqual([[
    'needs-permission', '--harness', 'claude', `--message=${message}`, '--interactive',
  ]])

  await $.classic.Notification({ message: 'Waiting for input', notification_type: 'idle_prompt' } as never)
  await clock.advance(0)
  expect(calls(h, 'needs-permission')).toHaveLength(1)

  h.reply['needs-permission'] = () => {
    throw new Error('meridian failed')
  }
  await $.classic.Notification({ message, notification_type: 'permission_prompt' } as never)
  await clock.advance(0)
  expect(calls(h, 'needs-permission')).toHaveLength(2)
})

test('child status refreshes on spawn-changing events and polls only while visible', async ($, on) => {
  const h = host()
  let childStatus = '2 running: p2 coder, p3 reviewer'
  h.reply['child-status'] = () => childStatus
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  expect(h.statuses).toEqual([childStatus])
  expect(calls(h, 'child-status')).toHaveLength(1)

  await clock.advance(60_000)
  expect(calls(h, 'child-status')).toHaveLength(2)

  await $.turn.complete(MAIN_TURN)
  await clock.advance(0)
  await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  await clock.advance(0)
  expect(calls(h, 'child-status')).toHaveLength(3)

  childStatus = ''
  await $.tool.call({ tool: 'Bash', command: 'meridian spawn wait' })
  await clock.advance(0)
  expect(h.statuses.at(-1)).toBeUndefined()
  expect(calls(h, 'child-status')).toHaveLength(4)

  await clock.advance(120_000)
  expect(calls(h, 'child-status')).toHaveLength(4)
})

test('a non-spawn Bash command does not refresh child status', async ($, on) => {
  const h = host()
  h.reply['child-status'] = '1 running: p2 coder'
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)

  await $.tool.call({ tool: 'Bash', command: 'git -C /work/meridian-cli status' })
  await clock.advance(0)

  expect(calls(h, 'child-status')).toHaveLength(1)
})

test('child status coalesces concurrent events into one trailing refresh', async ($, on) => {
  const h = host()
  const visible = '1 running: p2 coder'
  h.reply['child-status'] = visible
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)

  const pending = deferred<string>()
  h.reply['child-status'] = () => pending.promise
  await $.tool.call({ tool: 'Bash', command: 'meridian spawn --bg' })
  await Promise.resolve()
  expect(calls(h, 'child-status')).toHaveLength(2)

  await $.tool.call({ tool: 'Bash', command: 'meridian spawn cancel p2' })
  h.reply['child-status'] = ''
  pending.resolve(visible)
  await clock.advance(0)

  expect(calls(h, 'child-status')).toHaveLength(3)
  expect(h.statuses).toEqual([visible, visible, undefined])
})

test('session end rejects a late child status result', async ($, on) => {
  const h = host()
  const visible = '1 running: p2 coder'
  h.reply['child-status'] = visible
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)

  const pending = deferred<string>()
  h.reply['child-status'] = () => pending.promise
  await $.tool.call({ tool: 'Bash', command: 'meridian spawn --bg' })
  await Promise.resolve()
  expect(calls(h, 'child-status')).toHaveLength(2)

  await $.session.end({ reason: 'user_exit' })
  pending.resolve('2 running: p2 coder, p3 reviewer')
  await clock.advance(0)

  expect(calls(h, 'child-status')).toHaveLength(2)
  expect(h.statuses).toEqual([visible, undefined])
})

test('a refresh requested by session restart survives the old generation', async ($, on) => {
  const h = host()
  const visible = '1 running: p2 coder'
  h.reply['child-status'] = visible
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)

  const pending = deferred<string>()
  h.reply['child-status'] = () => pending.promise
  await $.tool.call({ tool: 'Bash', command: 'meridian spawn --bg' })
  await Promise.resolve()
  expect(calls(h, 'child-status')).toHaveLength(2)

  await $.session.end({ reason: 'clear' })
  await $.session.start(TUI)
  await clock.advance(0)
  h.reply['child-status'] = visible
  pending.resolve('stale old-session status')
  await clock.advance(0)

  expect(calls(h, 'child-status')).toHaveLength(3)
  expect(h.statuses).toEqual([visible, undefined, visible])
})

test('a disabled gate prevents child status and clears the line', async ($, on) => {
  const h = host()
  const config = deferred<{ enabled: boolean, reason: string }>()
  h.reply.config = () => config.promise
  h.reply['child-status'] = '1 running: p2 coder'
  const clock = install(on, h)

  await $.session.start(TUI)
  await $.tool.call({ tool: 'Bash', command: 'meridian spawn --bg' })
  config.resolve({ enabled: false, reason: 'role' })
  await clock.advance(0)

  expect(calls(h, 'child-status')).toHaveLength(0)
  expect(h.statuses).toEqual([undefined])
  await clock.advance(120_000)
  expect(calls(h, 'child-status')).toHaveLength(0)
})

test('a failed child status poll preserves the line and retries slowly', async ($, on) => {
  const h = host()
  const visible = '1 running: p2 coder'
  h.reply['child-status'] = visible
  const clock = install(on, h)
  await $.session.start(TUI)
  await clock.advance(2000)

  h.reply['child-status'] = () => { throw new Error('temporary failure') }
  await clock.advance(60_000)
  expect(calls(h, 'child-status')).toHaveLength(2)
  expect(h.statuses).toEqual([visible])

  h.reply['child-status'] = ''
  await clock.advance(60_000)
  expect(calls(h, 'child-status')).toHaveLength(3)
  expect(h.statuses).toEqual([visible, undefined])
})
