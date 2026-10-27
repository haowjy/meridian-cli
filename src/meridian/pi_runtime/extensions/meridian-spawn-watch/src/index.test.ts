import { mkdtemp, mkdir, readFile, rm, link } from 'node:fs/promises';
import { writeFileSync } from 'node:fs';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { afterEach, describe, expect, it, vi } from 'vitest';
import extension, { SpawnWatchRuntime } from './index';
import { writeJsonAtomic } from '../../shared/json_file';
import type { BashRecord } from '../../shared/schemas';
const formatter = vi.hoisted(() => vi.fn(async (args: string[]) => ({
  exitCode: 0, stdout: args.slice(2, -1).join(' '), stderr: ''
})));
vi.mock('../../shared/meridian_cli', () => ({
  runMeridianCommand: formatter
}));
const savedEnv = {
  ...process.env
};
const roots: string[] = [];
const owners: SpawnWatchRuntime[] = [];
const writers: ChildProcess[] = [];
type Notice = {
  customType: string;
  content: string;
  details: {
    delivery_id: string;
    work_ids: string[];
  };
};
type DeliveryOptions = {
  triggerTurn?: boolean;
  deliverAs?: 'steer' | 'followUp' | 'nextTurn';
};
async function pause(ms = 30) {
  await new Promise(r => setTimeout(r, ms));
}
async function eventually(check: () => boolean | Promise<boolean>) {
  for (let i = 0; i < 100; i++) {
    if (await check())
      return;
    await pause();
  }
  throw Error('delivery contract did not settle');
}
async function exists(filePath: string) {
  try { await readFile(filePath); return true; }
  catch { return false; }
}
function record(id: string, overrides: Partial<BashRecord> = {}): BashRecord {
  return {
    bash_id: id, command: `echo ${id}`, cwd: '/tmp', pid: null, status: 'exited', is_background: true, is_tracked: true, exit_code: 0, started_at_ms: Date.now() - 1000, ended_at_ms: Date.now(), log_path: '/unused', stdout_log_path: '/unused', stderr_log_path: '/unused', log_bytes: 0, timeout_min: 1, originating_bash_id: null, ...overrides
  };
}
async function setup() {
  const root = await mkdtemp(path.join(tmpdir(), 'delivery-contract-'));
  roots.push(root);
  process.env._MERIDIAN_PI_STATE_DIR = root;
  process.env.MERIDIAN_SPAWN_ID = 'p-parent';
  return root;
}
async function file(root: string, name: string, value: unknown) {
  await writeJsonAtomic(path.join(root, 'pi-bash', 'p-parent', name), value);
}
async function observations(root: string, value: unknown) {
  await writeJsonAtomic(path.join(root, 'spawns', 'p-parent', 'observed-spawns.json'), value);
}
async function bash(root: string, ...records: BashRecord[]) {
  await file(root, 'bash-records.json', {
    v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(), records: Object.fromEntries(records.map(r => [r.bash_id, r]))
  });
}
async function child(
  root: string,
  id: string,
  originating_bash_id: string | null = null,
  overrides: Record<string, unknown> = {},
) {
  // Captured from Python TerminalFacts.model_dump(); preserve canonical extras
  // so a handwritten projection cannot hide reader/writer incompatibility.
  const canonical = JSON.parse(await readFile(new URL('../fixtures/canonical-child-state.json', import.meta.url), 'utf8'));
  const terminal = overrides.terminal && typeof overrides.terminal === 'object'
    ? { ...canonical.terminal, ...overrides.terminal }
    : overrides.terminal === null ? null : canonical.terminal;
  await writeJsonAtomic(path.join(root, 'spawns', id, 'state.json'), {
    ...canonical, id, parent_id: 'p-parent', originating_bash_id, ...overrides, terminal,
  });
}
function holdReport(root: string, id: string, text = `report for ${id}`) {
  const reportPath = path.join(root, 'spawns', id, 'report.md');
  const enteredPath = `${reportPath}.entered`;
  const releasePath = `${reportPath}.release`;
  execFileSync('mkfifo', [reportPath]);
  const writer = spawn('bash', ['-c', [
    'exec 3>"$REPORT"',
    ': >"$ENTERED"',
    'while [[ ! -e "$RELEASE" ]]; do sleep 0.01; done',
    'printf %s "$TEXT" >&3',
  ].join('; ')], {
    env: { ...process.env, REPORT: reportPath, ENTERED: enteredPath, RELEASE: releasePath, TEXT: text },
    stdio: 'ignore',
  });
  writers.push(writer);
  return { reportPath, enteredPath, releasePath };
}
function chunkedReport(root: string, id: string, first: string, second: string) {
  const reportPath = path.join(root, 'spawns', id, 'report.md');
  const firstWrittenPath = `${reportPath}.first-written`;
  const releasePath = `${reportPath}.release`;
  execFileSync('mkfifo', [reportPath]);
  const writer = spawn('bash', ['-c', [
    'exec 3>"$REPORT"',
    'printf %s "$FIRST" >&3',
    ': >"$FIRST_WRITTEN"',
    'while [[ ! -e "$RELEASE" ]]; do sleep 0.01; done',
    'printf %s "$SECOND" >&3',
  ].join('; ')], {
    env: { ...process.env, REPORT: reportPath, FIRST_WRITTEN: firstWrittenPath,
      RELEASE: releasePath, FIRST: first, SECOND: second },
    stdio: 'ignore',
  });
  writers.push(writer);
  return { firstWrittenPath, releasePath };
}
function host(idle = true) {
  const h = {
    idle, notices: [] as Notice[], deliveries: [] as DeliveryOptions[], handlers: new Map<string, Function>(),
    commands: new Map<string, {handler: Function}>(), pi: {} as ExtensionAPI
  };
  h.pi = {
    on: (name: string, callback: Function) => h.handlers.set(name, callback),
    registerCommand: (name: string, command: {handler: Function}) => h.commands.set(name, command),
    sendMessage: (message: Notice, options?: DeliveryOptions) => {
      h.notices.push(message);
      h.deliveries.push(options ?? {});
      h.idle = false;
    }
  } as unknown as ExtensionAPI;
  return h;
}
function owner(h: ReturnType<typeof host>) {
  const runtime = new SpawnWatchRuntime(h.pi, () => h.idle);
  owners.push(runtime);
  runtime.start();
  return runtime;
}
afterEach(async () => {
  for (const writer of writers.splice(0)) {
    if (writer.exitCode === null) writer.kill('SIGKILL');
  }
  for (const runtime of owners.splice(0))
    runtime.stop();
  const registry = globalThis as unknown as {
    [key: symbol]: Map<string, SpawnWatchRuntime>;
  };
  for (const runtime of registry[Symbol.for('meridian.spawn-watch.runtimes')]?.values() ?? [])
    runtime.stop();
  registry[Symbol.for('meridian.spawn-watch.runtimes')]?.clear();
  await pause(100);
  for (const root of roots.splice(0))
    await rm(root, {
      recursive: true, force: true
    });
  for (const key of Object.keys(process.env))
    if (!(key in savedEnv))
      delete process.env[key];
  Object.assign(process.env, savedEnv);
  formatter.mockReset();
  formatter.mockImplementation(async (args: string[]) => ({
    exitCode: 0, stdout: args.slice(2, -1).join(' '), stderr: ''
  }));
});
describe('durable result delivery', () => {
  it('keeps a Bash infrastructure fault in its automatic result notice', async () => {
    const root = await setup();
    await bash(root, record('b-00000001', { status: 'killed', exit_code: -1, execution_error: 'EIO: result publication failed' }));
    const h = host(false);
    const runtime = owner(h);
    await runtime.deliverAtBoundary();
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.content).toContain('EIO: result publication failed');
    expect(h.notices[0]!.details.work_ids).toEqual(['b-00000001']);
  });

  it('does not scan parent-local evidence on active timer ticks', async () => {
    const root = await setup();
    await file(root, 'bash-records.json', { records: [] });
    const h = host(false);
    owner(h);

    await pause(800);

    await expect(readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'))).rejects.toThrow();
    expect(h.notices).toHaveLength(0);
  });

  it('stops an idle discovery before its next row when the agent starts', async () => {
    const root = await setup();
    const held = [];
    for (const id of ['p0', 'p1']) {
      await mkdir(path.join(root, 'spawns', id), { recursive: true });
      const row = holdReport(root, id, JSON.stringify({
        id, parent_id: 'p-other', status: 'running', terminal: null,
      }));
      await link(row.reportPath, path.join(root, 'spawns', id, 'state.json'));
      held.push(row);
    }
    const h = host(true);
    const runtime = owner(h);
    await eventually(() => exists(held[0]!.enteredPath));
    h.idle = false;
    runtime.observeActive(() => h.idle);
    writeFileSync(held[0]!.releasePath, 'release');
    await pause(300);

    // The first filesystem await may finish, but no next row may start reading.
    expect(await exists(held[1]!.enteredPath)).toBe(false);
    expect(h.notices).toHaveLength(0);
  });

  it('renders automatic spawn delivery directly from the durable report', async () => {
    const root = await setup();
    await child(root, 'p1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'direct report text');
    const h = host(false);
    const runtime = owner(h);

    await runtime.deliverAtBoundary(new AbortController().signal);

    expect(formatter).not.toHaveBeenCalled();
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.content).toContain('direct report text');
    expect(h.notices[0]!.details).not.toHaveProperty('ids');
  });

  it('renders failure and missing-report outcomes without hiding the result', async () => {
    const root = await setup();
    await child(root, 'p1', null, { status: 'failed', terminal: { exit_code: 1, error: 'worker failed' } });
    await child(root, 'p2');
    writeFileSync(path.join(root, 'spawns', 'p2', 'report.md'), '   ');
    const h = host(false);
    const runtime = owner(h);

    await runtime.deliverAtBoundary(new AbortController().signal);

    expect(h.notices[0]!.details.work_ids).toEqual(['p1', 'p2']);
    expect(h.notices[0]!.content).toContain('p1 failed');
    expect(h.notices[0]!.content).toContain('exit 1');
    expect(h.notices[0]!.content).toContain('Failure: worker failed');
    expect(h.notices[0]!.content.match(/No durable report content was published\./g)).toHaveLength(2);
    expect(h.notices[0]!.content).toContain('Transcript: meridian session log p1');
  });

  it('marks bounded report truncation with the full report path', async () => {
    const root = await setup();
    await child(root, 'p1');
    const reportPath = path.join(root, 'spawns', 'p1', 'report.md');
    writeFileSync(reportPath, `${'x'.repeat(70 * 1024)}TAIL`);
    const h = host(false);
    const runtime = owner(h);

    await runtime.deliverAtBoundary(new AbortController().signal);

    expect(h.notices[0]!.content).toContain(`[Report truncated after 65536 bytes; full report: ${reportPath}]`);
    expect(h.notices[0]!.content).not.toContain('TAIL');
  });

  it('reads a report through short reads until EOF', async () => {
    const root = await setup();
    await child(root, 'p1');
    const chunks = chunkedReport(root, 'p1', 'first chunk', ' and second chunk');
    const h = host(false);
    const runtime = owner(h);

    const boundary = runtime.deliverAtBoundary(new AbortController().signal);
    await eventually(() => exists(chunks.firstWrittenPath));
    writeFileSync(chunks.releasePath, 'release');
    await boundary;

    expect(h.notices[0]!.content).toContain('first chunk and second chunk');
  });

  it('fails closed on an unreadable present report and recovers after repair', async () => {
    const root = await setup();
    await child(root, 'p1');
    const reportPath = path.join(root, 'spawns', 'p1', 'report.md');
    await mkdir(reportPath);
    const h = host(false);
    const runtime = owner(h);

    await runtime.deliverAtBoundary(new AbortController().signal);
    expect(h.notices).toHaveLength(0);
    expect(JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8')).operation).toBe('scan');

    await rm(reportPath, { recursive: true });
    writeFileSync(reportPath, 'repaired report');
    await runtime.deliverAtBoundary(new AbortController().signal);
    expect(h.notices[0]!.content).toContain('repaired report');
  });

  it('steers unconsumed Bash results at a successful tool-bearing turn boundary', async () => {
    const root = await setup();
    await bash(root, record('b1', { notification_consumed_at_ms: Date.now() }), record('b2'));
    const h = host(false);
    extension(h.pi);
    h.handlers.get('session_start')!({}, { isIdle: () => false });

    await h.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [{}] }, {
      isIdle: () => false,
      signal: new AbortController().signal,
    });

    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.details.work_ids).toEqual(['b2']);
    expect(h.deliveries).toEqual([{ triggerTurn: true, deliverAs: 'steer' }]);
  });

  it('freshly discovers a child created during the tool turn at its boundary', async () => {
    const root = await setup();
    const h = host(false);
    const runtime = owner(h);
    await child(root, 'p1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'new child report');

    await runtime.deliverAtBoundary(new AbortController().signal);

    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('new child report');
  });

  it('serializes a fresh boundary discovery behind an older idle report read', async () => {
    const root = await setup();
    await child(root, 'p1');
    const held = holdReport(root, 'p1', 'old idle report');
    const h = host(true);
    const runtime = owner(h);
    await eventually(() => exists(held.enteredPath));

    h.idle = false;
    runtime.observeActive(() => h.idle);
    await child(root, 'p2');
    writeFileSync(path.join(root, 'spawns', 'p2', 'report.md'), 'second report');
    const boundary = runtime.deliverAtBoundary(new AbortController().signal);
    await rm(held.reportPath);
    writeFileSync(held.reportPath, 'fresh first report');
    writeFileSync(held.releasePath, 'release');
    await boundary;

    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1', 'p2']);
    expect(h.notices[0]!.content).toContain('fresh first report');
    expect(h.notices[0]!.content).toContain('second report');
    expect(h.deliveries[0]).toEqual({ triggerTurn: true, deliverAs: 'steer' });
  });

  it('does not publish at non-tool, error, aborted, or cancelled boundaries', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const h = host(false);
    extension(h.pi);
    h.handlers.get('session_start')!({}, { isIdle: () => false });
    const controller = new AbortController();
    const ctx = { isIdle: () => false, signal: controller.signal };

    await h.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [] }, ctx);
    await h.handlers.get('turn_end')!({ outcome: 'error', toolResults: [{}] }, ctx);
    await h.handlers.get('turn_end')!({ outcome: 'aborted', toolResults: [{}] }, ctx);
    controller.abort();
    await h.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [{}] }, ctx);

    expect(h.notices).toHaveLength(0);
  });

  it('keeps a result owed when the native boundary aborts during a report read', async () => {
    const root = await setup();
    await child(root, 'p1');
    const held = holdReport(root, 'p1');
    const h = host(false);
    const runtime = owner(h);
    const controller = new AbortController();
    const boundary = runtime.deliverAtBoundary(controller.signal);
    await eventually(() => exists(held.enteredPath));
    controller.abort();
    await rm(held.reportPath);
    writeFileSync(held.reportPath, 'report after abort');
    writeFileSync(held.releasePath, 'release');
    await boundary;
    expect(h.notices).toHaveLength(0);

    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.deliveries[0]).toEqual({ triggerTurn: true, deliverAs: 'followUp' });
  });

  it('invalidates an old boundary drain when reload rebinds the owner', async () => {
    const root = await setup();
    await child(root, 'p1');
    const held = holdReport(root, 'p1');
    const first = host(false);
    extension(first.pi);
    first.handlers.get('session_start')!({}, { isIdle: () => false });
    const oldBoundary = first.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [{}] }, {
      isIdle: () => false,
      signal: new AbortController().signal,
    });
    await eventually(() => exists(held.enteredPath));

    first.handlers.get('session_shutdown')!({ reason: 'reload' });
    const next = host(false);
    extension(next.pi);
    next.handlers.get('session_start')!({}, { isIdle: () => false });
    await rm(held.reportPath);
    writeFileSync(held.reportPath, 'report after reload');
    writeFileSync(held.releasePath, 'release');
    await oldBoundary;
    expect(first.notices).toHaveLength(0);

    await next.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [{}] }, {
      isIdle: () => false,
      signal: new AbortController().signal,
    });
    expect(next.notices).toHaveLength(1);
    expect(next.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(next.deliveries[0]).toEqual({ triggerTurn: true, deliverAs: 'steer' });
  });

  it('observes idle before the final snapshot when consumption finishes with the tool', async () => {
    const root = await setup();
    await child(root, 'p1');
    await mkdir(path.join(root, 'spawns', 'p-parent'), { recursive: true });
    const h = host(false);
    let idleChecks = 0;
    const runtime = new SpawnWatchRuntime(h.pi, () => {
      idleChecks++;
      if (idleChecks === 1) {
        // Model a consuming tool persisting its marker immediately before the
        // host reports idle. The final snapshot must happen after this effect.
        writeFileSync(path.join(root, 'spawns', 'p-parent', 'observed-spawns.json'), JSON.stringify({
          v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(),
          observed_spawn_ids: ['p1'], waiting_spawn_ids: [], wait_reservations: {},
        }));
        h.idle = true;
      }
      return h.idle;
    });
    owners.push(runtime);
    runtime.start();

    await eventually(() => idleChecks > 0);
    await pause(500);
    expect(h.notices).toHaveLength(0);
  });

  it('invalidates an idle report read across an active-to-idle ABA transition', async () => {
    const root = await setup();
    await child(root, 'p1');
    const held = holdReport(root, 'p1', 'stale idle report');
    const h = host(true);
    const runtime = owner(h);
    await eventually(() => exists(held.enteredPath));

    h.idle = false;
    runtime.observeActive(() => h.idle);
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await rm(held.reportPath);
    writeFileSync(held.reportPath, 'current idle report');
    writeFileSync(held.releasePath, 'release');

    await eventually(() => h.notices.length === 1);
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.content).toContain('current idle report');
    expect(h.notices[0]!.content).not.toContain('stale idle report');
    expect(h.deliveries[0]).toEqual({ triggerTurn: true, deliverAs: 'followUp' });
  });

  it('rereads a known candidate status on ordinary idle ticks', async () => {
    const root = await setup();
    await child(root, 'p1', null, { status: 'running', terminal: null });
    const h = host(true);
    owner(h);
    await pause(200);
    expect(h.notices).toHaveLength(0);

    await child(root, 'p1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'terminal after scoped poll');

    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('terminal after scoped poll');
  });

  it('periodically reconciles a child whose ownership change had no root event', async () => {
    const root = await setup();
    await child(root, 'p1', null, { parent_id: 'p-other' });
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'late ownership report');
    const h = host(false);

    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      const runtime = owner(h);
      expect(await runtime.rows()).toHaveLength(0);
      await child(root, 'p1');
      h.idle = true;
      runtime.observeIdle(() => h.idle);
      await pause(100);
      await vi.advanceTimersByTimeAsync(1_000);
      await pause(100);
      expect(h.notices).toHaveLength(0);

      // At 30s the ordinary poll may already own the single flight. Let it
      // settle, then the next 500ms tick observes the reconciliation request.
      await vi.advanceTimersByTimeAsync(29_000);
      await pause(100);
      await vi.advanceTimersByTimeAsync(500);
      await eventually(() => h.notices.length === 1);
      expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('revalidates selected children despite unrelated root churn during discovery', async () => {
    const root = await setup();
    await mkdir(path.join(root, 'spawns', 'p0'), { recursive: true });
    const held = holdReport(root, 'p0', JSON.stringify({
      id: 'p0', parent_id: 'p-other', status: 'running', terminal: null,
    }));
    // A FIFO-backed foreign row holds discovery while a real root event fires.
    await link(held.reportPath, path.join(root, 'spawns', 'p0', 'state.json'));
    await child(root, 'p1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'stable selected child');
    const h = host(false);
    const runtime = owner(h);
    const boundary = runtime.deliverAtBoundary();
    await eventually(() => exists(held.enteredPath));
    await mkdir(path.join(root, 'spawns', 'p-unrelated'));
    await pause();
    writeFileSync(held.releasePath, 'release');
    await boundary;

    expect(h.notices).toHaveLength(1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('stable selected child');
  });

  it.each([false, true])('defers superseded Bash correlation while preserving stable spawn=%s', async (withStableSpawn) => {
    const root = await setup();
    const foreign = { id: 'p0', parent_id: 'p-other', status: 'running', terminal: null };
    const statePath = path.join(root, 'spawns', 'p0', 'state.json');
    await writeJsonAtomic(statePath, foreign);
    if (withStableSpawn) await child(root, 'p2');
    const h = host(false);
    const runtime = owner(h);
    expect((await runtime.rows()).map(row => row.id)).toEqual(withStableSpawn ? ['p2'] : []);
    const held = holdReport(root, 'p0', JSON.stringify(foreign));
    await rm(statePath);
    await link(held.reportPath, statePath);
    await bash(root, record('b1'));
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await eventually(() => exists(held.enteredPath));

    // The canonical child arrives after the correlation scan's directory listing.
    await child(root, 'p1', 'b1', { status: 'running', terminal: null });
    await rm(statePath);
    await writeJsonAtomic(statePath, foreign);
    await pause();
    writeFileSync(held.releasePath, 'release');
    await pause(800);
    expect(h.notices.map(notice => notice.details.work_ids)).toEqual(withStableSpawn ? [['p2']] : []);

    await child(root, 'p1', 'b1');
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await eventually(() => h.notices.length === (withStableSpawn ? 2 : 1));
    expect(h.notices.at(-1)!.details.work_ids).toEqual(['p1']);
  });

  it('revalidates child correlation before sending a bare launcher result', async () => {
    const root = await setup();
    await child(root, 'p1', 'b1', { parent_id: 'p-other', status: 'running', terminal: null });
    const h = host(false);
    const runtime = owner(h);
    expect(await runtime.rows()).toEqual([]);

    // Canonical ownership changes inside an existing directory: no root event.
    // The candidate cache cannot prove that this launcher has no child.
    await child(root, 'p1', 'b1', { status: 'running', terminal: null });
    await bash(root, record('b1'));
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await pause(800);
    expect(h.notices).toHaveLength(0);

    await child(root, 'p1', 'b1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'child owns launcher outcome');
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('child owns launcher outcome');
  });

  it('keeps failed forced discovery dirty until a full discovery succeeds', async () => {
    const root = await setup();
    await child(root, 'p1', null, { status: 'running', terminal: null });
    await child(root, 'p2', null, { parent_id: 'p-other' });
    const h = host(false);
    const runtime = owner(h);
    expect((await runtime.rows()).map(row => row.id)).toEqual(['p1']);

    // Rewrite inside an existing foreign directory, which the nonrecursive
    // root watcher cannot observe. The boundary must make its failed forced
    // discovery sticky instead of falling back to the old {p1} candidate set.
    await writeJsonAtomic(path.join(root, 'spawns', 'p2', 'state.json'), {
      id: 'p2', parent_id: 'p-parent', status: 'succeeded', terminal: 'malformed',
    });
    await runtime.deliverAtBoundary(new AbortController().signal);
    let fault = JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8'));
    expect(fault.error).toContain('invalid spawn state: p2');

    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await pause(800);
    fault = JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8'));
    expect(fault.error).toContain('invalid spawn state: p2');
    expect(h.notices).toHaveLength(0);

    await child(root, 'p2');
    writeFileSync(path.join(root, 'spawns', 'p2', 'report.md'), 'repaired discovery report');
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p2']);
    await eventually(async () => JSON.parse(
      await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8')
    ).error === null);
  });

  it('scopes invalid terminal evidence to its canonical parent', async () => {
    const root = await setup();
    await child(root, 'p1');
    await writeJsonAtomic(path.join(root, 'spawns', 'p2', 'state.json'), {
      id: 'p2', parent_id: 'p-other', status: 'succeeded', terminal: 'malformed',
    });
    const runtime = new SpawnWatchRuntime(host().pi, () => true);
    owners.push(runtime);
    expect((await runtime.rows()).map(row => row.id)).toEqual(['p1']);
    await writeJsonAtomic(path.join(root, 'spawns', 'p2', 'state.json'), {
      id: 'p2', parent_id: 'p-parent', status: 'succeeded', terminal: 'malformed',
    });
    await expect(runtime.rows()).rejects.toThrow('invalid spawn state: p2');
  });

  it('uses framed UI notification for the no-UI slash listing', async () => {
    await setup();
    const h = host();
    extension(h.pi);
    const notify = vi.fn();
    const stdout = vi.spyOn(process.stdout, 'write');
    try {
      await h.commands.get('spawn')!.handler('', {hasUI: true, mode: 'rpc', ui: {notify, custom: async () => undefined}});
      expect(notify).toHaveBeenCalledWith('No correlated Meridian spawns.', 'info');
      expect(stdout).not.toHaveBeenCalled();
      expect(h.notices).toHaveLength(0);
    } finally { stdout.mockRestore(); }
  });
  it('does not trust expired or dead owner reservations after a restart', async () => {
    const root = await setup();
    await child(root, 'p1');
    await child(root, 'p2');
    await observations(root, {
      v: 1, spawn_id: 'p-parent', observed_spawn_ids: [], waiting_spawn_ids: ['p1', 'p2'],
      wait_reservations: {
        dead: {owner_pid: 2147483647, owner_birth_epoch: 1, expires_at_epoch: Date.now()/1000+30, spawn_ids: ['p1']},
        expired: {owner_pid: process.pid, owner_birth_epoch: 1, expires_at_epoch: 1, spawn_ids: ['p2']},
      },
    });
    const h = host();
    owner(h);
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1', 'p2']);
  });
  it('supervises a receipt write failure without claiming admission', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const h = host();
    const runtime = owner(h);
    await eventually(() => h.notices.length === 1);
    const receiptsPath = path.join(root, 'pi-bash', 'p-parent', 'delivery-receipts.json');
    await mkdir(receiptsPath);
    await expect(runtime.admitMessage({role: 'custom', ...h.notices[0]})).resolves.toBeUndefined();
    const fault = JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8'));
    expect(fault.operation).toBe('admission');
    expect(fault.error).toBeTruthy();
    runtime.stop();
    await rm(receiptsPath, {recursive: true});
    const restarted = host();
    const nextOwner = owner(restarted);
    await eventually(() => restarted.notices.length === 1);
    await nextOwner.admitMessage({role: 'custom', ...restarted.notices[0]});
    expect(JSON.parse(await readFile(receiptsPath, 'utf8')).messages).toHaveProperty(restarted.notices[0]!.details.delivery_id);
  });
  it('keeps streaming results owed and excludes waited members of a batch', async () => {
    const root = await setup();
    const h = host(false);
    const runtime = owner(h);
    await bash(root, record('b1'), record('b2'));
    await pause(700);
    expect(h.notices).toHaveLength(0);
    await bash(root, record('b1', {
      notification_consumed_at_ms: Date.now()
    }), record('b2'));
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['b2']);
    expect(h.notices[0]!.content).not.toContain('b1');
  });
  it('waited Bash produces zero follow-ups after the next idle turn', async () => {
    const root = await setup();
    const h = host(false);
    const runtime = owner(h);
    await bash(root, record('b1'));
    await pause(300);
    await bash(root, record('b1', {
      notification_consumed_at_ms: Date.now()
    }));
    h.idle = true;
    runtime.observeIdle(() => h.idle);
    await pause(900);
    expect(h.notices).toHaveLength(0);
  });
  it('queues once; exact admission becomes durable and survives cold restart', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const h = host();
    const runtime = owner(h);
    await eventually(() => h.notices.length === 1);
    await pause(700);
    expect(h.notices).toHaveLength(1);
    await runtime.admitMessage({
      role: 'custom', ...h.notices[0], details: {
        delivery_id: 'unrelated', work_ids: ['b1']
      }
    });
    await expect(readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-receipts.json'))).rejects.toThrow();
    await runtime.admitMessage({
      role: 'custom', ...h.notices[0]
    });
    runtime.stop();
    const receipt = JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-receipts.json'), 'utf8'));
    expect(Object.values(receipt.messages)).toEqual([['b1']]);
    const restarted = host();
    owner(restarted);
    await pause(900);
    expect(restarted.notices).toHaveLength(0);
  });
  it('cold restart retries unadmitted work', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const first = host();
    const runtime = owner(first);
    await eventually(() => first.notices.length === 1);
    runtime.stop();
    const next = host();
    owner(next);
    await eventually(() => next.notices.length === 1);
    expect(next.notices[0]!.details.work_ids).toEqual(['b1']);
  });
  it('delivers nothing at session start, then the first turn end delivers what is owed', async () => {
    const root = await setup();
    await child(root, 'p1');
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'owed before the resume');
    // A resumed session is idle with a cold prompt cache: an automatic turn here
    // would re-bill the whole context for a status ping.
    const h = host();
    extension(h.pi);
    h.handlers.get('session_start')!({ reason: 'resume' }, { isIdle: () => h.idle });
    await pause(800);
    expect(h.notices).toHaveLength(0);

    h.handlers.get('agent_end')!({}, { isIdle: () => h.idle });
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('owed before the resume');
    expect(h.deliveries[0]).toEqual({ triggerTurn: true, deliverAs: 'followUp' });
  });

  it('delivers a crashed generation\'s owed child after the resume', async () => {
    const root = await setup();
    // Core hands an interactive primary every primary generation of its chat.
    process.env._MERIDIAN_PI_OWED_PARENT_IDS = 'p-parent,p-crashed';
    await child(root, 'p1', null, { parent_id: 'p-crashed' });
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'result from the crashed generation');
    const h = host();
    extension(h.pi);
    h.handlers.get('session_start')!({ reason: 'resume' }, { isIdle: () => h.idle });
    await pause(800);
    expect(h.notices).toHaveLength(0);

    h.handlers.get('agent_end')!({}, { isIdle: () => h.idle });
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
    expect(h.notices[0]!.content).toContain('result from the crashed generation');

    // Admission stays on the current spawn; core unions its generations.
    await h.handlers.get('message_start')!({ message: { role: 'custom', ...h.notices[0] } });
    const receipt = JSON.parse(await readFile(
      path.join(root, 'pi-bash', 'p-parent', 'delivery-receipts.json'), 'utf8',
    ));
    expect(Object.values(receipt.messages)).toEqual([['p1']]);
  });

  it('does not redeliver a prior generation\'s child any generation consumed', async () => {
    const root = await setup();
    process.env._MERIDIAN_PI_OWED_PARENT_IDS = 'p-crashed,p-middle,p-parent';
    await child(root, 'p1', null, { parent_id: 'p-crashed' });
    await child(root, 'p2', null, { parent_id: 'p-crashed' });
    await child(root, 'p3', null, { parent_id: 'p-crashed' });
    await child(root, 'p4', null, { parent_id: 'p-crashed' });
    await writeJsonAtomic(path.join(root, 'pi-bash', 'p-crashed', 'delivery-receipts.json'), {
      v: 1, spawn_id: 'p-crashed', messages: { 'd-receipt': ['p1'] },
    });
    // A middle generation delivered p4 and crashed too: neither the child's parent nor the current spawn holds it.
    await writeJsonAtomic(path.join(root, 'pi-bash', 'p-middle', 'delivery-receipts.json'), {
      v: 1, spawn_id: 'p-middle', messages: { 'd-middle': ['p4'] },
    });
    await writeJsonAtomic(path.join(root, 'spawns', 'p-crashed', 'observed-spawns.json'), {
      v: 1, spawn_id: 'p-crashed', updated_at_ms: Date.now(), observed_spawn_ids: ['p2'],
    });
    const h = host();
    const runtime = owner(h);

    await runtime.deliverAtBoundary(new AbortController().signal);

    expect(h.notices[0]!.details.work_ids).toEqual(['p3']);
  });

  it('keeps the widened scope across a reload', async () => {
    const root = await setup();
    process.env._MERIDIAN_PI_OWED_PARENT_IDS = 'p-parent,p-crashed';
    await child(root, 'p1', null, { parent_id: 'p-crashed' });
    const first = host(false);
    extension(first.pi);
    first.handlers.get('session_start')!({ reason: 'startup' }, { isIdle: () => false });
    first.handlers.get('session_shutdown')!({ reason: 'reload' });

    const next = host(false);
    extension(next.pi);
    next.handlers.get('session_start')!({ reason: 'reload' }, { isIdle: () => false });
    await next.handlers.get('turn_end')!({ outcome: 'completed', toolResults: [{}] }, {
      isIdle: () => false,
      signal: new AbortController().signal,
    });

    expect(next.notices[0]!.details.work_ids).toEqual(['p1']);
  });

  it('leaves a child its launcher waited for to the launcher result', async () => {
    const root = await setup();
    await child(root, 'p1', null, { launch_mode: 'foreground' });
    writeFileSync(path.join(root, 'spawns', 'p1', 'report.md'), 'shown synchronously');
    const h = host();
    const runtime = owner(h);

    await pause(800);

    // The launcher already has this result; /spawn still lists the child.
    expect(h.notices).toHaveLength(0);
    expect((await runtime.rows()).map(row => row.id)).toEqual(['p1']);
  });

  it('keeps the launching Bash result when its foreground child is not owed', async () => {
    const root = await setup();
    await child(root, 'p1', 'b1', { launch_mode: 'foreground' });
    await bash(root, record('b1'));
    const h = host();

    await owner(h).deliverAtBoundary(new AbortController().signal);

    expect(h.notices[0]!.details.work_ids).toEqual(['b1']);
    expect(h.notices[0]!.content).toContain('Background bash b1');
  });

  it('stops using the old idle capability between reload shutdown and rebind', async () => {
    await setup();
    const h = host();
    extension(h.pi);
    const idle = vi.fn(() => true);
    h.handlers.get('session_start')!({}, { isIdle: idle });
    h.handlers.get('agent_end')!({}, { isIdle: idle });
    await pause(100);
    expect(idle).toHaveBeenCalled();
    h.handlers.get('session_shutdown')!({ reason: 'reload' });

    // Native Pi invalidates this context before awaiting resource reload. Return
    // false rather than throwing so the regression reports an assertion failure.
    idle.mockClear();
    idle.mockImplementation(() => false);
    await pause(700);
    expect(idle).not.toHaveBeenCalled();
  });

  it('reload retains queued ownership until the retained native message is admitted', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const first = host();
    extension(first.pi);
    first.handlers.get('session_start')!({ reason: 'startup' }, {
      isIdle: () => first.idle
    });
    first.handlers.get('agent_end')!({}, {
      isIdle: () => first.idle
    });
    await eventually(() => first.notices.length === 1);
    first.handlers.get('session_shutdown')!({
      reason: 'reload'
    });
    const next = host(false);
    extension(next.pi);
    next.handlers.get('session_start')!({ reason: 'reload' }, {
      isIdle: () => next.idle
    });
    await pause(800);
    expect(next.notices).toHaveLength(0);
    await next.handlers.get('message_start')!({
      message: {
        role: 'custom', ...first.notices[0]
      }
    });
    next.idle = true;
    next.handlers.get('agent_end')!({}, {
      isIdle: () => next.idle
    });
    await pause(800);
    expect(next.notices).toHaveLength(0);
  });
  it('temporary wait reservations become eligible again when removed', async () => {
    const root = await setup();
    await child(root, 'p1');
    const birth = Date.parse(execFileSync('ps', ['-o', 'lstart=', '-p', String(process.pid)], {
      encoding: 'utf8'
    }).trim()) / 1000;
    const observation = {
      v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(), observed_spawn_ids: [], waiting_spawn_ids: ['p1'], wait_reservations: {
        live: {
          owner_pid: process.pid, owner_birth_epoch: birth, expires_at_epoch: Date.now() / 1000 + 30, spawn_ids: ['p1']
        }
      }
    };
    await observations(root, observation);
    const h = host();
    owner(h);
    await pause(800);
    expect(h.notices).toHaveLength(0);
    await observations(root, {
      ...observation, wait_reservations: {}, waiting_spawn_ids: []
    });
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
  });
  it.each([
    { path: 'idle', idle: true, deliverAs: 'followUp' },
    { path: 'boundary', idle: false, deliverAs: 'steer' },
  ])('rechecks observations after report reads on the $path path', async ({ idle, deliverAs }) => {
    const root = await setup();
    await child(root, 'p1');
    await child(root, 'p2');
    const held = holdReport(root, 'p1', 'consumed report');
    writeFileSync(path.join(root, 'spawns', 'p2', 'report.md'), 'still owed report');
    const h = host(idle);
    const runtime = owner(h);
    const delivery = idle ? undefined : runtime.deliverAtBoundary(new AbortController().signal);
    await eventually(() => exists(held.enteredPath));
    await observations(root, {
      v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(), observed_spawn_ids: ['p1'], wait_reservations: {}
    });
    writeFileSync(held.releasePath, 'release');
    await delivery;
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p2']);
    expect(h.notices[0]!.content).toContain('still owed report');
    expect(h.notices[0]!.content).not.toContain('consumed report');
    expect(h.deliveries[0]).toEqual({ triggerTurn: true, deliverAs });
  });
  it('supervises malformed file failures and recovers after repair', async () => {
    const root = await setup();
    await file(root, 'bash-records.json', {
      records: []
    });
    const h = host();
    owner(h);
    await eventually(async () => {
      try {
        return !!JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8')).error;
      }
      catch {
        return false;
      }
    });
    expect(h.notices).toHaveLength(0);
    await bash(root, record('b1'));
    await eventually(() => h.notices.length === 1);
    await eventually(async () => JSON.parse(await readFile(path.join(root, 'pi-bash', 'p-parent', 'delivery-fault.json'), 'utf8')).error === null);
  });
});
