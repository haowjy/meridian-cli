import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
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
type Notice = {
  customType: string;
  content: string;
  details: {
    delivery_id: string;
    work_ids: string[];
  };
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
async function bash(root: string, ...records: BashRecord[]) {
  await file(root, 'bash-records.json', {
    v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(), records: Object.fromEntries(records.map(r => [r.bash_id, r]))
  });
}
async function child(root: string, id: string, originating_bash_id: string | null = null) {
  // Captured from Python TerminalFacts.model_dump(); preserve canonical extras
  // so a handwritten projection cannot hide reader/writer incompatibility.
  const canonical = JSON.parse(await readFile(new URL('../fixtures/canonical-child-state.json', import.meta.url), 'utf8'));
  await writeJsonAtomic(path.join(root, 'spawns', id, 'state.json'), {
    ...canonical, id, parent_id: 'p-parent', originating_bash_id,
  });
}
function host(idle = true) {
  const h = {
    idle, notices: [] as Notice[], handlers: new Map<string, Function>(),
    commands: new Map<string, {handler: Function}>(), pi: {} as ExtensionAPI
  };
  h.pi = {
    on: (name: string, callback: Function) => h.handlers.set(name, callback),
    registerCommand: (name: string, command: {handler: Function}) => h.commands.set(name, command),
    sendMessage: (message: Notice) => {
      h.notices.push(message);
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
    await file(root, 'observed-spawns.json', {
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
  it('reload retains queued ownership until the retained native message is admitted', async () => {
    const root = await setup();
    await bash(root, record('b1'));
    const first = host();
    extension(first.pi);
    first.handlers.get('session_start')!({}, {
      isIdle: () => first.idle
    });
    await eventually(() => first.notices.length === 1);
    first.handlers.get('session_shutdown')!({
      reason: 'reload'
    });
    const next = host(false);
    extension(next.pi);
    next.handlers.get('session_start')!({}, {
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
    await file(root, 'observed-spawns.json', observation);
    const h = host();
    owner(h);
    await pause(800);
    expect(h.notices).toHaveLength(0);
    await file(root, 'observed-spawns.json', {
      ...observation, wait_reservations: {}, waiting_spawn_ids: []
    });
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p1']);
  });
  it('rechecks observations after formatter await and preserves the rest of the batch', async () => {
    const root = await setup();
    await child(root, 'p1');
    await child(root, 'p2');
    let entered = false;
    let release!: () => void;
    formatter.mockImplementationOnce(async () => {
      entered = true;
      await new Promise<void>(r => {
        release = r;
      });
      return {
        exitCode: 0, stdout: 'p1 p2', stderr: ''
      };
    });
    const h = host();
    owner(h);
    await eventually(() => entered);
    await file(root, 'observed-spawns.json', {
      v: 1, spawn_id: 'p-parent', updated_at_ms: Date.now(), observed_spawn_ids: ['p1'], wait_reservations: {}
    });
    release();
    await eventually(() => h.notices.length === 1);
    expect(h.notices[0]!.details.work_ids).toEqual(['p2']);
    expect(h.notices[0]!.content).toBe('p2');
  });
  it('continuous writes cannot postpone a scan indefinitely', async () => {
    const root = await setup();
    const h = host();
    owner(h);
    let stopped = false;
    const churn = (async () => {
      while (!stopped) {
        await bash(root, record('b1'));
        await pause(20);
      }
    })();
    try {
      await eventually(() => h.notices.length === 1);
    }
    finally {
      stopped = true;
      await churn;
    }
    expect(h.notices).toHaveLength(1);
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
