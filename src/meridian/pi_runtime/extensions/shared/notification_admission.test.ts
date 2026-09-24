import { describe, expect, it } from "vitest";

import { NOTIFICATION_GATE_ENV, notificationAdmission } from "./notification_admission";

function capabilityEnv(attempt: string, nonce: string): NodeJS.ProcessEnv {
  return {
    [NOTIFICATION_GATE_ENV.version]: "1",
    [NOTIFICATION_GATE_ENV.attempt]: attempt,
    [NOTIFICATION_GATE_ENV.nonce]: nonce,
  };
}

describe("managed notification admission", () => {
  it("keeps ordinary untracked extension behavior enabled", () => {
    const gate = notificationAdmission({});
    expect(gate.tracked).toBe(false);
    expect(gate.allows()).toBe(true);
  });

  it("fails closed on an incorrect capability and removes it from child env", () => {
    const env = { ...capabilityEnv("attempt", "bad-capability"), [NOTIFICATION_GATE_ENV.version]: "9" };
    const gate = notificationAdmission(env);
    expect(gate.tracked).toBe(true);
    expect(gate.valid).toBe(false);
    expect(gate.allows()).toBe(false);
    expect(Object.values(NOTIFICATION_GATE_ENV).some((name) => env[name] !== undefined)).toBe(false);
  });

  it("releases once per admitted run, revokes on selection changes, and never reopens after close", () => {
    const gate = notificationAdmission(capabilityEnv("attempt", `nonce-${Math.random()}`));
    expect(gate.allows()).toBe(false);
    const firstRun = gate.agentStart();
    expect(gate.allows(firstRun)).toBe(true);
    gate.suspend();
    expect(gate.allows(firstRun)).toBe(false);
    expect(gate.allows(gate.agentStart())).toBe(true);
    gate.close();
    expect(gate.allows()).toBe(false);
    gate.agentStart();
    expect(gate.allows()).toBe(false);
  });
});
