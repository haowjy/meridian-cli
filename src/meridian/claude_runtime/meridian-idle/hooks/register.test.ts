import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'

import { COMPACT_INSTRUCTIONS, parseChoice } from './keepwarm'

type Host = {
  calls: string[][]
  reply: Record<string, unknown | ((args: string[]) => unknown)>
  /** Texts the mod submitted as plugin prompts. */
  submitted: string[]
  /** What `$.prompt.read()` answers. */
  box: string
  /** The plugin store as the mod last wrote it. */
  store: Record<string, unknown>
  /** How long `fire` takes to answer, standing in for a timer the machine ran late. */
  fireDelayMs?: number
  /** What the host answers a compaction with; the default is a finished compaction. */
  compact?: () => unknown
  /** The `instructions` of each compaction that reached the host. */
  compactions: unknown[]
}

const START = 1_000_000
const TUI = { cwd: '/work/project', surface: 'terminal', isInteractive: true } as const
const MAIN_TURN = { answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' } as const
const COMPOSER = { kind: 'composer' } as const

function host(): Host {
  return {
    calls: [],
    submitted: [],
    compactions: [],
    box: '',
    store: {},
    reply: {
      config: { enabled: true, warn_minutes: 10 },
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
  on('process.run', async (_$, e) => {
    const args = e.argv.slice(2)
    h.calls.push([...args])
    if (args[0] === 'fire' && h.fireDelayMs !== undefined) await clock.sleep(h.fireDelayMs)
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
  on('session.end', () => ({ sessionId: 'sess-1' }) as never)
  on('session.compact', (_$, e) => {
    h.compactions.push(e.instructions)
    return (h.compact?.() ?? { messages: [SUMMARY] }) as never
  })
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
  // `activity` is reported whatever the idle gate says, so it asserts no TUI ownership.
  expect(h.calls.every(args => args[0] === 'activity' || args.at(-1) === '--interactive')).toBe(true)
  for (const args of h.calls.filter(args => ['config', 'arm', 'return', 'fire'].includes(args[0] ?? ''))) {
    expect(flag(args, '--harness')).toBe('claude')
    if (args[0] !== 'config') expect(flag(args, '--session')).toBe('sess-1')
  }
}

test('print mode keeps the mod inert', async ($, on) => {
  const h = host()
  const clock = install(on, h)

  await $.session.start({ cwd: '/work/project', surface: null, isInteractive: false })
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete(MAIN_TURN)
  await $.prompt.submit({ text: 'hi', wait: false, origin: COMPOSER })
  await $.command.run({ command: 'compact', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } })
  await clock.advance(100_000)

  expect(h.calls).toEqual([])
})

/** The turn edges Meridian heard, in order: `activity <state>`, and each arm as the turn end it carries. */
function edges(h: Host): string[] {
  return h.calls.flatMap(args => (args[0] === 'activity' ? [args[1] ?? ''] : args[0] === 'arm' ? ['idle (arm)'] : []))
}

for (const enabled of [true, false]) {
  test(`main-loop turn edges reach Meridian in order with idle ${enabled ? 'on' : 'off'}`, async ($, on) => {
    const h = host()
    h.reply.config = { enabled, reason: enabled ? undefined : 'idle-disabled', warn_minutes: 10 }
    h.reply.activity = { recorded: true }
    const clock = install(on, h)

    await $.session.start(TUI)
    await $.turn.start({ text: 'go', turnId: 't1' })
    // A subagent's turn is not the session's.
    await $.turn.start({ text: 'review', turnId: 'a1', agentId: 'agent-1' })
    await $.turn.complete({ ...MAIN_TURN, turnId: 'a1', agentId: 'agent-1' })
    await $.turn.complete({ ...MAIN_TURN, reason: 'aborted', isAborted: true })
    await clock.advance(0)

    // With idle off nothing arms, so the turn end is reported on its own.
    expect(edges(h)).toEqual(['idle', 'turn_active', enabled ? 'idle (arm)' : 'idle'])
    expect(calls(h, 'activity').every(args => flag(args, '--harness') === 'claude')).toBe(true)
  })
}

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
const KEEPWARM_COMMAND = { command: 'keepwarm', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } } as const
const LEAD = 20_000
const SUMMARY = { role: 'user', text: 'summary', toolUses: [] } as const
const COMPACTION = { messages: [SUMMARY] } as never

/** A fake core: the first arm places the warning LEAD ahead, later arms place nothing, and a return opens a new stretch. */
function keepwarmHost(lead = LEAD): Host {
  const h = host()
  let armed = 0
  let stretch = 1
  h.reply.arm = () => {
    armed += 1
    return armed === 1 ? { stretch, anchor: armed, warn_at: START + 2000 + lead } : { stretch, anchor: armed }
  }
  h.reply.return = () => {
    stretch += 1
    return { stretch_closed: true }
  }
  return h
}

async function startKeepwarm($: Engine, clock: ReturnType<typeof install>, answer = 'done') {
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.command.run({ ...KEEPWARM_COMMAND, args: 'on' })
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete({ ...MAIN_TURN, answer })
}

/** The model's turn for the ping the mod just submitted. */
async function pingTurn($: Engine, h: Host, turnId: string, answer: string, reason: 'answer' | 'aborted' = 'answer') {
  await $.turn.start({ text: h.submitted.at(-1) ?? '', turnId })
  await $.turn.complete({ ...MAIN_TURN, turnId, answer, reason, isAborted: reason === 'aborted' } as never)
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
  expect(h.submitted[0]).toMatch(/^\[meridian-idle keepwarm [a-z0-9]+\]\n\nWaiting for the user, keeping warm\. To stop, reply with exactly stop-waiting and nothing else: that ends this stretch's pings, so you stop waiting for the user and accept the cache going cold before they return\.$/)

  const deny = (input: object) => $.tool.check({ tool: 'Edit', input: {}, tool_use_id: 'tu-1', ...input } as never)
  expect((await deny({})).decision).toBe('allow') // before the ping turn starts
  await $.turn.start({ text: h.submitted[0] ?? '', turnId: 'p1' })
  expect((await deny({})).decision).toBe('deny')
  expect((await deny({ isReadOnly: true })).decision).toBe('allow')
  expect((await deny({ agentId: 'sub-1' })).decision).toBe('allow')
  expect((await deny({ tool_use_id: undefined })).decision).toBe('allow') // another plugin's permission query
  await $.turn.complete({ ...MAIN_TURN, turnId: 'p1', answer: '`stop-waiting`' })
  await clock.advance(0)
  expect((await deny({})).decision).toBe('allow') // after it

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

test('a ping held by a draft gives up before the cache expires, however long the lead', async ($, on) => {
  const MINUTE = 60_000
  const h = keepwarmHost(50 * MINUTE) // a 1-hour cache: the warning comes 50 minutes after the turn, 10 before expiry
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'a draft'
  await clock.advance(50 * MINUTE + 5 * MINUTE)
  expect(h.submitted).toEqual([])
  await clock.advance(5 * MINUTE) // minute 60: expired
  h.box = '' // the user clears the box at minute 70
  await clock.advance(10 * MINUTE)
  expect(h.submitted).toEqual([])
})

test('a warning acted on 90 s late gets no ping once a draft holds the box to the expiry', async ($, on) => {
  const MINUTE = 60_000
  const h = keepwarmHost(50 * MINUTE)
  h.fireDelayMs = 90_000 // core accepts a warning up to 120 s late
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'a draft'
  await clock.advance(60 * MINUTE - 1000) // just before the cache expires
  h.box = ''
  await clock.advance(30_000)
  expect(h.submitted).toEqual([])
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

test('a turn running when the warning fires skips the ping and spends no slot; its arm chains the ping a lead later', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await $.turn.start({ text: 'task notification', turnId: 't2' }) // runs through the warning
  await clock.advance(LEAD)
  expect(h.submitted).toEqual([])
  expect(calls(h, 'fire').map(args => args[1])).toEqual(['warn'])
  await $.turn.complete({ ...MAIN_TURN, turnId: 't2', answer: 'built it' })
  await clock.advance(LEAD - 1)
  expect(h.submitted).toEqual([]) // not before the lead has passed
  await clock.advance(1)
  expect(h.submitted).toHaveLength(1)
  expect(calls(h, 'arm')[1]).toContain('--assistant-text=built it')

  await pingTurn($, h, 'p1', 'ok')
  await clock.advance(0)
  expect(calls(h, 'arm')[2]).toContain('--assistant-text=built it')
  const status = await $.command.run({ ...IDLE_COMMAND, args: '' })
  expect(status.text).toContain('keepwarm: on, 1/3 pings')
})

test('a user message while a turn runs at the warning leaves no ping behind', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await $.turn.start({ text: 'task notification', turnId: 't2' })
  await clock.advance(LEAD)
  await $.prompt.submit({ text: 'back', wait: false, origin: COMPOSER })
  await $.turn.complete({ ...MAIN_TURN, turnId: 't2' })
  await clock.advance(10 * LEAD)
  expect(h.submitted).toEqual([])
})

test('an aborted ping turn still counts', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', '', 'aborted')

  await clock.advance(LEAD)
  expect(h.submitted).toHaveLength(2) // the cap counts pings, not outcomes
})

test('keepwarm off stops pings, and /meridian-idle prints the state', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'stop-waiting')
  const status = await $.command.run({ ...IDLE_COMMAND, args: '' })
  expect(status.text).toContain('keepwarm: on, 3/3 pings this stretch')

  const off = await $.command.run({ ...KEEPWARM_COMMAND, args: 'off' })
  expect(off.text).toBe('keepwarm: off, 3/3 pings this stretch')
  expect((await $.command.run({ ...IDLE_COMMAND, args: 'keepwarm off' })).text).toContain('usage: /meridian-idle')
  expect(calls(h, 'return')).toEqual([])
})

test("a choice is the reply's last line, alone or in one pair of quotes", () => {
  for (const reply of ['stop-waiting', '  stop-waiting\n', '"stop-waiting"', "'stop-waiting'", '`stop-waiting`', '“stop-waiting”', '‘stop-waiting’']) {
    expect(parseChoice(reply)).toBe('stop-waiting')
  }
  expect(parseChoice('Work item updated.\n\n`compact-now`\n')).toBe('compact-now')
  for (const reply of ['stop-waiting.', 'I will stop-waiting', '""stop-waiting""', '"stop-waiting”', '', 'ok', 'compact-now\nthen more', 'compact-now is my choice']) {
    expect(parseChoice(reply)).toBeUndefined()
  }
})

/** Like `startKeepwarm`, but without `/keepwarm on`: only core's config answer can turn it on. */
async function startConfigured($: Engine, clock: ReturnType<typeof install>) {
  await $.session.start(TUI)
  await clock.advance(2000)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete(MAIN_TURN)
}

test('the config default turns keepwarm on without /keepwarm on, and /keepwarm off still wins', async ($, on) => {
  const h = keepwarmHost()
  h.reply.config = { enabled: true, warn_minutes: 10, keepwarm: true }
  const clock = install(on, h)

  await startConfigured($, clock)
  await clock.advance(LEAD)
  expect(h.submitted).toHaveLength(1)
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on, 1/3')

  await $.command.run({ ...KEEPWARM_COMMAND, args: 'off' })
  await pingTurn($, h, 'p1', 'ok')
  await clock.advance(10 * LEAD)
  expect(h.submitted).toHaveLength(1)
})

test('/clear drops the /keepwarm off override and the config default returns', async ($, on) => {
  const h = keepwarmHost()
  h.reply.config = { enabled: true, warn_minutes: 10, keepwarm: true }
  const clock = install(on, h)
  // `$.state` outlives a hot reload but not `/clear`: hide what was written before the clear.
  let cleared = false
  on('state.get', (_$, e, next) => (cleared ? { value: { value: undefined } } : next(e)))

  await startConfigured($, clock)
  await $.command.run({ ...KEEPWARM_COMMAND, args: 'off' })
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: off')

  await $.session.end({ reason: 'clear' } as never)
  cleared = true
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on')
})

for (const config of [{ enabled: true, warn_minutes: 10, keepwarm: false }, { enabled: true, warn_minutes: 10 }]) {
  test(`keepwarm ${JSON.stringify(config)} in the config reply keeps it off`, async ($, on) => {
    const h = keepwarmHost()
    h.reply.config = config
    const clock = install(on, h)

    await startConfigured($, clock)
    await clock.advance(LEAD)
    expect(h.submitted).toEqual([])
  })
}

for (const trigger of ['auto', 'plugin'] as const) {
  test(`a main-conversation ${trigger} compaction ends the stretch's pings`, async ($, on) => {
    const h = keepwarmHost()
    const clock = install(on, h)

    await startKeepwarm($, clock)
    await clock.advance(LEAD)
    await pingTurn($, h, 'p1', 'ok') // ping 2 is now pending on the chained timer
    await $.session.compact({ trigger, ...COMPACTION } as never)
    await clock.advance(10 * LEAD)

    expect(h.submitted).toHaveLength(1)
    const status = (await $.command.run({ ...IDLE_COMMAND, args: '' })).text ?? ''
    expect(status).toContain('keepwarm: on, 3/3 pings this stretch')
    expect(status).toContain(`compaction (${trigger})`)
  })
}

test('a compaction while ping 1 waits for the box sends no ping', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'a draft'
  await clock.advance(LEAD + 5000)
  await $.session.compact({ trigger: 'auto', ...COMPACTION } as never)
  h.box = ''
  await clock.advance(10 * LEAD)
  expect(h.submitted).toEqual([])
})

test('a skipped compaction leaves the pings alone and returns the host result', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)
  h.compact = () => ({ skip: 'blocked by a PreCompact hook' })

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'ok')
  const result = await $.session.compact({ trigger: 'auto', ...COMPACTION } as never)
  await clock.advance(LEAD)

  expect(result).toEqual({ skip: 'blocked by a PreCompact hook' })
  expect(h.submitted).toHaveLength(2)
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on, 2/3')
})

for (const outcome of ['skipped', 'landed'] as const) {
  test(`a ping due while a compaction runs waits; a ${outcome} compaction ${outcome === 'skipped' ? 'lets it go out' : 'ends it'}`, async ($, on) => {
    const h = keepwarmHost()
    const clock = install(on, h)
    let finish: () => void = () => undefined
    const gate = new Promise<void>(resolve => { finish = resolve })
    h.compact = () => gate.then(() => (outcome === 'skipped' ? { skip: 'blocked' } : { messages: [SUMMARY] }))

    await startKeepwarm($, clock)
    await clock.advance(1) // the arm has reached core
    const compaction = $.session.compact({ trigger: 'auto', ...COMPACTION } as never)
    await clock.advance(LEAD - 1)
    expect(h.submitted).toEqual([])
    expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on, 0/3')
    finish()
    await compaction
    await clock.advance(5000)

    expect(h.submitted).toHaveLength(outcome === 'skipped' ? 1 : 0)
  })
}

test('a ping waiting for the box through a skipped compaction goes out once it clears', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)
  let finish: () => void = () => undefined
  const gate = new Promise<void>(resolve => { finish = resolve })

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'ok')
  h.box = 'a draft'
  await clock.advance(LEAD + 1) // ping 2 is due and waiting for the box
  h.compact = () => gate.then(() => ({ skip: 'blocked' }))
  const compaction = $.session.compact({ trigger: 'auto', ...COMPACTION } as never)
  h.box = ''
  await clock.advance(30_000) // quiet long enough, but the compaction holds the ping
  expect(h.submitted).toHaveLength(1)
  finish()
  await compaction
  await clock.advance(5000)

  expect(h.submitted).toHaveLength(2)
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on, 2/3')
})

for (const event of [{ trigger: 'precompute' }, { trigger: 'auto', agentId: 'sub-1' }]) {
  test(`a compaction ${JSON.stringify(event)} changes nothing`, async ($, on) => {
    const h = keepwarmHost()
    const clock = install(on, h)

    await startKeepwarm($, clock)
    await clock.advance(LEAD)
    await pingTurn($, h, 'p1', 'ok')
    await $.session.compact({ ...event, ...COMPACTION } as never)
    await clock.advance(LEAD)
    expect(h.submitted).toHaveLength(2)
  })
}

test('keepwarm_pings in the config reply sets the limit: 1 gives exactly one ping', async ($, on) => {
  const h = keepwarmHost()
  h.reply.config = { enabled: true, warn_minutes: 10, keepwarm: true, keepwarm_pings: 1 }
  const clock = install(on, h)

  await startConfigured($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'ok')
  await clock.advance(10 * LEAD)

  expect(h.submitted).toHaveLength(1)
  expect((await $.command.run({ ...IDLE_COMMAND, args: '' })).text).toContain('keepwarm: on, 1/1 pings this stretch')
})

test('a cold deadline ends the stretch\'s pings: a later arm does not ping', async ($, on) => {
  const MINUTE = 60_000
  const h = keepwarmHost(50 * MINUTE)
  const clock = install(on, h)

  await startKeepwarm($, clock)
  h.box = 'a draft'
  await clock.advance(60 * MINUTE) // past dueAt + warn - 1 min: the cache would be cold
  h.box = ''
  await clock.advance(MINUTE)
  expect(h.submitted).toEqual([])
  const status = (await $.command.run({ ...IDLE_COMMAND, args: '' })).text ?? ''
  expect(status).toContain('keepwarm: on, 3/3 pings this stretch')
  expect(status).toContain("prompts are over: the cache would be cold")

  await $.turn.start({ text: 'later', turnId: 't2' })
  await $.turn.complete({ ...MAIN_TURN, turnId: 't2' })
  await clock.advance(90 * MINUTE)
  expect(h.submitted).toEqual([])
})

for (const how of ['session.end clear', 'a /clear command'] as const) {
  test(`${how} cancels a pending chained ping`, async ($, on) => {
    const h = keepwarmHost()
    const clock = install(on, h)

    await startKeepwarm($, clock)
    await clock.advance(LEAD)
    await pingTurn($, h, 'p1', 'ok') // ping 2 is pending on the chained timer
    expect(h.submitted).toHaveLength(1)
    if (how === 'session.end clear') await $.session.end({ reason: 'clear' } as never)
    else await $.command.run({ command: 'clear', args: '', origin: COMPOSER, presentation: { isFullscreen: true, columns: 80 } } as never)
    await clock.advance(10 * LEAD)

    expect(h.submitted).toHaveLength(1)
  })
}

// -- compact-now on the state reminder ---------------------------------------------

/** A fake core that sends the reminder with the warning. */
function reminderHost(): Host {
  const h = keepwarmHost()
  h.reply.fire = { decision: 'act', reason: 'guards-passed', remind: true }
  return h
}

async function statusOf($: Engine): Promise<string> {
  return (await $.command.run({ ...IDLE_COMMAND, args: '' })).text ?? ''
}

for (const keepwarm of [true, false]) {
  test(`the reminder offers compact-now, and stop-waiting only with keepwarm on (${keepwarm ? 'on' : 'off'})`, async ($, on) => {
    const h = reminderHost()
    const clock = install(on, h)
    if (keepwarm) await startKeepwarm($, clock)
    else await startConfigured($, clock)
    await clock.advance(LEAD)

    expect(h.submitted).toHaveLength(1)
    expect(h.submitted[0]).toContain('compact-now')
    expect(h.submitted[0]?.includes('stop-waiting')).toBe(keepwarm)
  })
}

test('compact-now after the reminder compacts once with the instructions and ends the pings', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'Work item updated.\ncompact-now')
  await clock.advance(10 * LEAD)

  expect(h.compactions).toEqual([COMPACT_INSTRUCTIONS])
  expect(h.submitted).toHaveLength(1)
  const status = await statusOf($)
  expect(status).toContain('keepwarm: on, 3/3 pings this stretch')
  expect(status).toContain('compacted')
  expect(calls(h, 'arm')).toHaveLength(2) // the reminder's own arm still reaches core
})

test('compact-now works with keepwarm off', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startConfigured($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(LEAD)

  expect(h.compactions).toHaveLength(1)
})

for (const [answer, pings] of [['Updated.\nstop-waiting', 1], ['Updated.\ncompact-now is not needed', 2]] as const) {
  test(`reminder reply ${JSON.stringify(answer)} ${pings === 1 ? 'ends the pings' : 'keeps waiting'} and compacts nothing`, async ($, on) => {
    const h = reminderHost()
    const clock = install(on, h)

    await startKeepwarm($, clock)
    await clock.advance(LEAD)
    await pingTurn($, h, 'p1', answer)
    await clock.advance(10 * LEAD)

    expect(h.compactions).toEqual([])
    expect(h.submitted).toHaveLength(pings)
  })
}

test('a ping answering compact-now compacts nothing', async ($, on) => {
  const h = keepwarmHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(LEAD)

  expect(h.compactions).toEqual([])
  expect(h.submitted).toHaveLength(2)
})

test('compact-now waits QUIET_MS after the turn, and after a later prompt change', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(9000)
  expect(h.compactions).toEqual([])
  // A draft typed and deleted restarts the quiet window.
  await $.prompt.edit({ text: 'x', cursor: 1, origin: COMPOSER } as never)
  await clock.advance(9000)
  expect(h.compactions).toEqual([])
  await clock.advance(2000)
  expect(h.compactions).toEqual([COMPACT_INSTRUCTIONS])
})

test("compact-now gives up on a draft at the reminder's cold-cache deadline", async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  h.box = 'a draft'
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(20 * 60_000)
  h.box = ''
  await clock.advance(20_000)
  expect(h.compactions).toEqual([])
  expect(await statusOf($)).toContain('compact-now skipped: the cache would be cold')
})

test("compact-now's deadline runs from the end of the reminder's turn, however long that turn took", async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await $.turn.start({ text: h.submitted.at(-1) ?? '', turnId: 'p1' })
  await clock.advance(9 * 60_000) // warn_minutes is 10: the reminder's own deadline is long gone
  await $.turn.complete({ ...MAIN_TURN, turnId: 'p1', answer: 'compact-now' } as never)
  await clock.advance(20_000)

  expect(h.compactions).toHaveLength(1)
})

test('compact-now compacts nothing when another compaction lands during its wait', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(1000)
  await $.session.compact({ trigger: 'auto', ...COMPACTION } as never)
  await clock.advance(30_000)

  expect(h.compactions).toHaveLength(1) // the auto one
})

test('compact-now is skipped when a main-loop turn is running at the end of the wait', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(5000)
  await $.turn.start({ text: 'task notification', turnId: 't9' })
  await clock.advance(20_000)

  expect(h.compactions).toEqual([])
  expect(await statusOf($)).toContain('compact-now skipped: a turn is running')
})

test('compact-now waits QUIET_MS after a turn shorter than the poll', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await pingTurn($, h, 'p1', 'compact-now')
  await clock.advance(9500)
  await $.turn.start({ text: 'task notification', turnId: 't9' })
  await $.turn.complete({ ...MAIN_TURN, turnId: 't9' })
  await clock.advance(1000)
  expect(h.compactions).toEqual([])
  await clock.advance(10_000)
  expect(h.compactions).toEqual([COMPACT_INSTRUCTIONS])
})

test('compact-now is skipped when the user came back during the reminder turn', async ($, on) => {
  const h = reminderHost()
  const clock = install(on, h)

  await startKeepwarm($, clock)
  await clock.advance(LEAD)
  await $.turn.start({ text: h.submitted[0] ?? '', turnId: 'p1' })
  await $.prompt.submit({ text: 'back', wait: false, origin: COMPOSER })
  await $.turn.complete({ ...MAIN_TURN, turnId: 'p1', answer: 'compact-now' })
  await clock.advance(LEAD)

  expect(h.compactions).toEqual([])
  expect(await statusOf($)).toContain('compact-now skipped: the user returned')
})

for (const how of ['vetoed', 'rejected'] as const) {
  test(`a ${how} compaction is logged and leaves the pings ended`, async ($, on) => {
    const h = reminderHost()
    const clock = install(on, h)
    h.compact = () => {
      if (how === 'rejected') throw new Error('a turn is running')
      return { skip: 'blocked by a PreCompact hook' }
    }

    await startKeepwarm($, clock)
    await clock.advance(LEAD)
    await pingTurn($, h, 'p1', 'compact-now')
    await clock.advance(10 * LEAD)

    expect(h.submitted).toHaveLength(1)
    const status = await statusOf($)
    expect(status).toContain('keepwarm: on, 3/3 pings this stretch')
    expect(status).toContain(how === 'vetoed' ? 'compaction skipped: blocked by a PreCompact hook' : 'compaction failed')
   })
}
