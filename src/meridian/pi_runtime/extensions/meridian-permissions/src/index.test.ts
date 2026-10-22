import { afterEach, describe, expect, it } from "vitest";

import meridianPermissionsExtension, {
  decideToolCall,
  type PermissionPolicy,
  type PermissionRule,
  splitShellCommands,
} from "./index.js";

const HEADLESS_REASON = "Needs approval, and a headless spawn has no one to ask. Do not retry it.";

function policy(
  rules: PermissionRule[],
  overrides: Partial<PermissionPolicy> = {},
): PermissionPolicy {
  return {
    version: 1,
    rules,
    allowlist_tools: [],
    completion_commands: [],
    headless_reject_reason: HEADLESS_REASON,
    ...overrides,
  };
}

function rule(overrides: Partial<PermissionRule>): PermissionRule {
  return {
    capability: "bash",
    tool: "bash",
    pattern: null,
    action: "allow",
    label: "bash",
    ...overrides,
  };
}

afterEach(() => {
  delete process.env._MERIDIAN_PI_PERMISSION_POLICY;
  delete process.env.MERIDIAN_SESSION_ROLE;
});

describe("permission precedence", () => {
  it("chooses the most specific matching scope", () => {
    const decision = decideToolCall(
      policy([
        rule({ pattern: "rg *", action: "deny", label: "bash(rg *)" }),
        rule({ pattern: "rg src/*", action: "allow", label: "bash(rg src/*)" }),
      ]),
      "bash",
      { command: "rg src/file.ts" },
    );

    expect(decision?.action).toBe("allow");
  });

  it("chooses deny over ask over allow at equal specificity", () => {
    const actions = ["allow", "ask", "deny"] as const;
    const decision = decideToolCall(
      policy(actions.map((action) => rule({ pattern: "rg *", action, label: action }))),
      "bash",
      { command: "rg needle" },
    );

    expect(decision?.action).toBe("deny");
  });

  it("chooses write over edit for Pi's shared write tool", () => {
    const decision = decideToolCall(
      policy([
        rule({ capability: "edit", tool: "write", pattern: "src/*", action: "allow" }),
        rule({ capability: "write", tool: "write", pattern: "src/*", action: "deny" }),
      ]),
      "write",
      { path: "src/file.ts" },
    );

    expect(decision?.action).toBe("deny");
  });

  it("blocks unmatched calls in a scoped-only allowlist", () => {
    const decision = decideToolCall(
      policy(
        [rule({ pattern: "rg *", action: "allow" })],
        { allowlist_tools: ["bash"] },
      ),
      "bash",
      { command: "touch forbidden" },
    );

    expect(decision).toMatchObject({ action: "deny" });
  });
});

describe("bash command evaluation", () => {
  const restrictive = policy([
    rule({ pattern: "rg *", action: "allow", label: "bash(rg *)" }),
    rule({ pattern: "touch *", action: "deny", label: "bash(touch *)" }),
  ]);

  it("checks every command in a compound expression", () => {
    expect(decideToolCall(restrictive, "bash", { command: "rg x; touch y" })?.action).toBe("deny");
    expect(decideToolCall(restrictive, "bash", { command: "echo $(touch y)" })?.action).toBe("deny");
  });

  it("does not split separators inside quotes", () => {
    expect(splitShellCommands("rg 'x; y'")).toEqual({ commands: ["rg 'x; y'"], complete: true });
  });

  it("allows a fixed completion command but still checks compound commands", () => {
    const withCompletion = policy(
      [rule({ capability: "*", tool: "*", action: "deny", label: "*" })],
      { completion_commands: ["meridian spawn done"] },
    );
    expect(decideToolCall(withCompletion, "bash", { command: "meridian spawn done p-1" })?.action).toBe("allow");
    expect(
      decideToolCall(withCompletion, "bash", { command: "meridian spawn done p-1; touch y" })?.action,
    ).toBe("deny");
  });

  it("fails closed when a restricted tool's scope cannot be read", () => {
    expect(decideToolCall(restrictive, "bash", {})?.action).toBe("deny");
    expect(
      decideToolCall(
        policy([rule({ tool: "future", pattern: "safe/*", action: "allow" })]),
        "future",
        { path: "safe/file" },
      )?.action,
    ).toBe("deny");
  });
});

it("blocks headless ask with the exact reason", async () => {
  process.env.MERIDIAN_SESSION_ROLE = "spawned";
  process.env._MERIDIAN_PI_PERMISSION_POLICY = JSON.stringify(
    policy([rule({ action: "ask" })]),
  );
  const handlers = new Map<string, (...args: any[]) => any>();
  meridianPermissionsExtension({
    on: (name: string, handler: (...args: any[]) => any) => handlers.set(name, handler),
  } as any);

  const blocked = await handlers.get("tool_call")?.(
    { toolCallId: "call-1", toolName: "bash", input: { command: "echo hi" } },
    { mode: "rpc", ui: { confirm: async () => true } },
  );
  expect(blocked).toEqual({ block: true, reason: HEADLESS_REASON });
});

it("routes primary ask through Pi's TUI confirmation", async () => {
  process.env.MERIDIAN_SESSION_ROLE = "primary";
  process.env._MERIDIAN_PI_PERMISSION_POLICY = JSON.stringify(
    policy([rule({ action: "ask" })]),
  );
  const handlers = new Map<string, (...args: any[]) => any>();
  meridianPermissionsExtension({
    on: (name: string, handler: (...args: any[]) => any) => handlers.set(name, handler),
  } as any);
  const confirmations: string[] = [];

  const result = await handlers.get("tool_call")?.(
    { toolCallId: "call-1", toolName: "bash", input: { command: "echo hi" } },
    {
      mode: "tui",
      ui: {
        confirm: async (_title: string, detail: string) => {
          confirmations.push(detail);
          return true;
        },
      },
    },
  );

  expect(result).toBeUndefined();
  expect(confirmations).toEqual(["echo hi"]);
});
