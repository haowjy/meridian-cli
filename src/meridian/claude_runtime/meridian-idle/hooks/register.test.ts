import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'

import { isStop } from './keepwarm'

type Host = {
  calls: string[][]
  reply: Record<string, unknown | ((args: string[]) => unknown)>
  /** Texts the mod submitted as plugin prompts. */
  submitted: string[]
  /** What `$.prompt.read()` answers. */
  box: string
  /** The plugin store as the mod last wrote it. */
  store: Record<string, unknown>
}

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function host(): Host {
  return {
    calls: [],
    submitted: [],
    box: '',
    store: {},
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
  on('store.get', (_$, e) => ({ value: h.store[e.key] }))
  on('store.set', (_$, e) => {
    h.store[e.key] = JSON.parse(JSON.stringify(e.value))
    return { value: undefined }
  })
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
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  on('classic.Stop', () => ({}))
  on('classic.Notification', () => ({}))
  on('prompt.submit', (_$, e) => {
    if (e.origin.kind === 'plugin') h.submitted.push(e.text)
    return { text: e.text }
  })
  on('prompt.edit', (_$, e) => e as never)
  on('prompt.read', () => ({ value: { text: h.box, cursor: 0 } }))
  on('tool.check', () => ({ decision: 'allow' }))
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

test('a subagent turn starting between the Stop and turn.complete does not drop the Stop list', async ($, on) => {
  const h = host()
  h.reply.arm = { stretch: 1, anchor: 1, warn_at: START + 20_000, push_held: '1 agent(s) running' }
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.classic.Stop({
    stop_hook_active: false,
    background_tasks: [{ id: 'a', type: 'subagent', status: 'running', description: 'review' }],
    session_crons: [],
  } as never)
  // A background subagent's run, then a compaction fork, start before the main loop's turn completes.
  await $.turn.start({ text: 'review the diff', turnId: 'a1', agentId: 'agent-1' })
  await $.turn.start({ text: 'summarize', turnId: 'a2', agentId: 'fork-1' })
  await $.turn.complete(MAIN_TURN)
  await clock.advance(0)

  const tasksArg = calls(h, 'arm')[0]?.find(arg => arg.startsWith('--background-tasks='))
  expect(JSON.parse(tasksArg?.slice('--background-tasks='.length) ?? 'null')).toEqual([{ kind: 'agent' }])
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

// -- keepwarm ---------------------------------------------------------------

const IDLE_COMMAND = { command: 'meridian-idle', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } } as const
const LEAD = 20_000

/** A fake core: the first arm of a stretch places the warning LEAD ahead, later arms of it place nothing. */
function keepwarmHost(): Host {
  const h = host()
  let armed = 0
  h.reply.arm = () => {
    armed += 1
    return armed === 1 ? { stretch: 1, anchor: armed, warn_at: START + 2000 + LEAD } : { stretch: 1, anchor: armed }
  }
  return h
}

async function startKeepwarm($: Engine, clock: ReturnType<typeof install>, answer = 'done') {
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.command.run({ ...IDLE_COMMAND, args: 'keepwarm on' })
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete({ ...MAIN_TURN, answer })
}

/** The model's turn for the ping the mod just submitted. */
async function pingTurn($: Engine, h: Host, turnId: string, answer: string, reason: 'answer' | 'aborted' = 'answer') {
  await $.turn.start({ text: h.submitted.at(-1) ?? '', turnId })
  await $.turn.complete({ ...MAIN_TURN, turnId, answer, reason, isAborted: reason === 'aborted' } as never)
}

function pingRecords(h: Host) {
  return (h.store.keepwarm ?? []) as Array<{ ping: number; outcome: string; denied: string[]; reply: string; why?: string }>
}

test('keepwarm is off until asked: the warning sends no ping', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.complete(MAIN_TURN)
  await clock.advance(LEAD)

  expect(calls(h, 'fire').map(args => args[1])).toEqual(['warn'])
  expect(h.submitted).toEqual([])
})

test('ping 1 follows the warning; its turn is found by nonce, its tools are limited, a stop ends the pings', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  // A keepwarm command is not a return.
  expect(calls(h, 'return')).toEqual([])
  await clock.advance(LEAD)
  expect(h.submitted).toHaveLength(1)
  expect(h.submitted[0]).toMatch(/^\[meridian-idle keepwarm [a-z0-9]+\]\n\nWaiting for the user, keeping warm\./)

  const deny = (input: object) => $.tool.check({ tool: 'Edit', input: {}, ...input } as never)
  expect((await deny({})).decision).toBe('allow') // before the ping turn starts
  await $.turn.start({ text: h.submitted[0] ?? '', turnId: 'p1' })
  expect((await deny({})).decision).toBe('deny')
  expect((await deny({ isReadOnly: true })).decision).toBe('allow')
  expect((await deny({ agentId: 'sub-1' })).decision).toBe('allow')
  await $.turn.complete({ ...MAIN_TURN, turnId: 'p1', answer: '`stop-waiting`' })
  await clock.advance(0)
  expect((await deny({})).decision).toBe('allow') // after it

  expect(pingRecords(h)).toMatchObject([{ ping: 1, outcome: 'stopped', denied: ['Edit'], reply: '`stop-waiting`' }])
  // The ping's arm carries the real answer of the last working turn, not the ping's reply.
  expect(calls(h, 'arm')[1]).toContain('--assistant-text=done')
  await clock.advance(10 * LEAD)
  expect(h.submitted).toHaveLength(1)
})

test('a continued ping chains one more at the lead after its arm, three in all', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  for (const [turnId, answer] of [['p1', 'ok'], ['p2', 'Still waiting.'], ['p3', 'ok']] as const) {
    expect(h.submitted).toHaveLength(Number(turnId.slice(1)))
    await pingTurn($, h, turnId, answer)
    await clock.advance(LEAD - 1)
    expect(h.submitted).toHaveLength(Number(turnId.slice(1))) // not before the lead has passed
    await clock.advance(1)
  }
  await clock.advance(10 * LEAD)

  expect(h.submitted).toHaveLength(3)
  expect((pingRecords(h)).map(r => [r.ping, r.outcome])).toEqual([[1, 'continued'], [2, 'continued'], [3, 'continued']])
  expect(calls(h, 'fire').map(args => args[1])).toEqual(['warn'])
})

test('a user return cancels a ping that waits for the box and the chained ping', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'half a sentence'
  await clock.advance(LEAD + 30_000)
  expect(h.submitted).toEqual([]) // the box is busy
  await $.prompt.submit({ text: 'back', wait: false, origin: COMPOSER })
  h.box = ''
  await clock.advance(60_000)
  expect(h.submitted).toEqual([]) // the user came back while the ping waited
})

test('a user return after ping 1 cancels the chained ping', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'ok')
  await clock.advance(LEAD - 1000)
  await $.prompt.submit({ text: 'back', wait: false, origin: COMPOSER })
  await clock.advance(10 * LEAD)
  expect(h.submitted).toHaveLength(1)
})

test('the ping waits until the box has been empty for ten seconds', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'typing'
  await clock.advance(LEAD + 5000)
  h.box = ''
  await ($.prompt as unknown as { edit: (e: object) => Promise<unknown> }).edit({ origin: { kind: 'user' }, text: '', inputText: '', cursor: 0, start: 0, end: 0 } as never)
  await clock.advance(9000)
  expect(h.submitted).toEqual([])
  await clock.advance(2000)
  expect(h.submitted).toHaveLength(1)
})

test('a turn already running when the warning fires is not mistaken for the ping', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await $.turn.start({ text: 'task notification', turnId: 't2' }) // runs through the warning
  await clock.advance(LEAD)
  expect(h.submitted).toHaveLength(1)
  await $.turn.complete({ ...MAIN_TURN, turnId: 't2', answer: 'built it' })
  await clock.advance(0)
  expect(pingRecords(h)).toEqual([])
  expect((await $.tool.check({ tool: 'Edit', input: {} } as never)).decision).toBe('allow')
  expect(calls(h, 'arm')[1]).toContain('--assistant-text=built it')

  await pingTurn($, h, 'p1', 'ok')
  await clock.advance(0)
  expect(pingRecords(h)).toMatchObject([{ ping: 1, outcome: 'continued' }])
  expect(calls(h, 'arm')[2]).toContain('--assistant-text=built it')
})

test('an aborted ping turn is logged failed and still counts', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', '', 'aborted')

  expect(pingRecords(h)).toMatchObject([{ ping: 1, outcome: 'failed', why: 'turn aborted' }])
  await clock.advance(LEAD)
  expect(h.submitted).toHaveLength(2) // the cap counts pings, not outcomes
})

test('keepwarm off stops pings, and /meridian-idle prints the state and the tally', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'stop-waiting')
  const status = await $.command.run({ ...IDLE_COMMAND, args: '' })
  expect(status.text).toContain('keepwarm: on, 3/3 pings this stretch')
  expect(status.text).toContain('pings logged: 1 stopped, 0 continued, 0 failed')

  const off = await $.command.run({ ...IDLE_COMMAND, args: 'keepwarm off' })
  expect(off.text).toContain('keepwarm: off')
  expect((await $.command.run({ ...IDLE_COMMAND, args: 'bogus' })).text).toContain('usage')
  expect(calls(h, 'return')).toEqual([])
})

test('a stop is the whole reply, alone or in one pair of quotes', () => {
  for (const reply of ['stop-waiting', '  stop-waiting\n', '"stop-waiting"', "'stop-waiting'", '`stop-waiting`', '“stop-waiting”', '‘stop-waiting’']) {
    expect(isStop(reply)).toBe(true)
  }
  for (const reply of ['stop-waiting.', 'I will stop-waiting', '""stop-waiting""', '"stop-waiting”', '', 'ok']) {
    expect(isStop(reply)).toBe(false)
  }
})
