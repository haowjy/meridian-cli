/**
 * Process-scoped causal gate for managed extension follow-up turns.
 *
 * The explicit launch capability is a mode signal only. It is not identity or
 * entry authority; the owner must commit entry before sending its first prompt.
 */
export const NOTIFICATION_GATE_ENV = {
  version: "_MERIDIAN_PI_NOTIFICATION_GATE_VERSION",
  attempt: "_MERIDIAN_PI_NOTIFICATION_GATE_ATTEMPT",
  nonce: "_MERIDIAN_PI_NOTIFICATION_GATE_NONCE",
} as const;

export type NotificationAdmission = {
  readonly tracked: boolean;
  readonly valid: boolean;
  readonly revision: number;
  allows(revision?: number): boolean;
  agentStart(): number;
  suspend(): void;
  close(): void;
};

type GateState = {
  attempt: string | null;
  nonce: string | null;
  valid: boolean;
  released: boolean;
  closed: boolean;
  revision: number;
};

const registrySymbol = Symbol.for("meridian.pi.notification-admission.v1");
const currentSymbol = Symbol.for("meridian.pi.notification-admission.current.v1");
type Registry = Map<string, GateState>;
const globalScope = globalThis as typeof globalThis & { [registrySymbol]?: Registry; [currentSymbol]?: GateState };
const gates = globalScope[registrySymbol] ??= new Map<string, GateState>();
const invalidGateKey = "invalid-launch-capability";

/** Capture and erase the owner-supplied capability before child processes inherit it. */
export function notificationAdmission(env: NodeJS.ProcessEnv = process.env): NotificationAdmission {
  const names = Object.values(NOTIFICATION_GATE_ENV);
  const supplied = names.some((name) => env[name] !== undefined);
  const values = Object.fromEntries(names.map((name) => [name, env[name]]));
  for (const name of names) delete env[name];

  // No capability means the extension is running in its ordinary untracked
  // mode. Tracked launch projection is responsible for making the capability
  // mandatory; a partial/incorrect capability is always fail-closed.
  if (!supplied) return globalScope[currentSymbol] ? trackedAdmission(globalScope[currentSymbol]!) : untrackedAdmission();
  const attempt = values[NOTIFICATION_GATE_ENV.attempt];
  const nonce = values[NOTIFICATION_GATE_ENV.nonce];
  const valid = values[NOTIFICATION_GATE_ENV.version] === "1" &&
    bounded(attempt) && bounded(nonce);
  const key = valid ? nonce : invalidGateKey;
  let state = gates.get(key);
  if (!state) {
    state = { attempt: valid ? attempt : null, nonce: valid ? nonce : null, valid, released: false, closed: false, revision: 0 };
    gates.set(key, state);
  } else if (!valid || state.attempt !== attempt || state.nonce !== nonce) {
    state.valid = false;
    state.released = false;
    state.closed = true;
    state.revision++;
  }
  globalScope[currentSymbol] = state;
  return trackedAdmission(state);
}

function trackedAdmission(state: GateState): NotificationAdmission {
  return {
    tracked: true,
    get valid() { return state.valid; },
    get revision() { return state.revision; },
    allows(revision = state.revision) { return state.valid && state.released && !state.closed && revision === state.revision; },
    agentStart() {
      if (state.valid && !state.closed && !state.released) {
        state.released = true;
        state.revision++;
      }
      return state.revision;
    },
    suspend() {
      if (state.closed) return;
      state.released = false;
      state.revision++;
    },
    close() {
      if (state.closed) return;
      state.released = false;
      state.closed = true;
      state.revision++;
    },
  };
}

function untrackedAdmission(): NotificationAdmission {
  return {
    tracked: false,
    valid: true,
    revision: 0,
    allows: () => true,
    agentStart: () => 0,
    suspend: () => undefined,
    close: () => undefined,
  };
}

function bounded(value: string | undefined): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 256 && !/[\u0000-\u001f]/.test(value);
}
