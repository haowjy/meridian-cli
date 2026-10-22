import { mkdirSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

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
      policy([
        rule({ pattern: "rg *", action: "allow" }),
        rule({ action: "deny", label: "bash outside its scoped allows" }),
      ]),
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

  it.each([
    "rg $'x\\''; printf hidden; printf %s $'y\\''",
    "rg $(printf hidden)",
    "rg `printf hidden`",
    "rg $HOME",
    "rg x > output",
    "rg x < input",
    "rg x <<EOF\nhidden\nEOF",
    "rg {x,y}",
    "rg <(printf hidden)",
    "rg x & printf hidden",
  ])("refuses unsupported shell syntax: %s", (command) => {
    expect(decideToolCall(restrictive, "bash", { command })).toMatchObject({
      action: "deny",
      reason: expect.stringContaining("run simple commands"),
    });
  });

  it("accepts the modeled simple-command subset", () => {
    expect(
      decideToolCall(restrictive, "bash", {
        command: "rg 'x; y' && rg \"z | q\"\nrg final",
      })?.action,
    ).toBe("allow");
  });

  it("allows a fixed completion command but still checks compound commands", () => {
    const withCompletion = policy(
      [rule({ capability: "*", tool: "*", action: "deny", label: "*" })],
      { completion_commands: ["meridian spawn done", "meridian spawn rearm"] },
    );
    for (const command of [
      "meridian spawn done",
      "meridian spawn done p-1",
      "meridian spawn rearm --candidate_1.v2",
    ]) {
      expect(decideToolCall(withCompletion, "bash", { command })?.action).toBe("allow");
    }
    for (const command of [
      "meridian spawn done --flag=value",
      "meridian spawn done 'p-1'",
      "meridian spawn done p/1",
      "meridian spawn donex",
      "meridian spawn done p-1; touch y",
      "meridian spawn done; meridian spawn rearm",
      "meridian spawn done > output",
    ]) {
      expect(decideToolCall(withCompletion, "bash", { command })?.action).toBe("deny");
    }
  });

  it("does not restrict ordinary bash when completion is the only policy", () => {
    const completionOnly = policy([], {
      completion_commands: ["meridian spawn done", "meridian spawn rearm"],
    });

    expect(
      decideToolCall(completionOnly, "bash", { command: "printf ordinary > output" }),
    ).toBeUndefined();
    expect(
      decideToolCall(completionOnly, "bash", { command: "meridian spawn done > output" }),
    ).toBeUndefined();
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

describe("path scope evaluation", () => {
  it("normalizes paths without changing OpenCode's recursive star semantics", () => {
    const root = mkdtempSync(join(tmpdir(), "meridian-pi-path-"));
    const project = join(root, "project");
    const outside = join(root, "outside");
    mkdirSync(join(project, "src", "nested"), { recursive: true });
    mkdirSync(outside);
    try {
      const scoped = policy([
        rule({ capability: "read", tool: "read", pattern: "src/*", action: "allow" }),
        rule({ capability: "read", tool: "read", action: "deny" }),
      ]);

      expect(
        decideToolCall(scoped, "read", { path: "src/nested/file.ts" }, project)?.action,
      ).toBe("allow");
      expect(
        decideToolCall(scoped, "read", { path: join(project, "src", "file.ts") }, project)?.action,
      ).toBe("allow");
      expect(
        decideToolCall(scoped, "read", { path: "src/../../outside/secret" }, project)?.action,
      ).toBe("deny");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("does not let a symlink carry a scoped allow outside its canonical root", () => {
    const root = mkdtempSync(join(tmpdir(), "meridian-pi-symlink-"));
    const project = join(root, "project");
    const outside = join(root, "outside");
    mkdirSync(join(project, "src"), { recursive: true });
    mkdirSync(outside);
    symlinkSync(outside, join(project, "src", "link"));
    symlinkSync(join(outside, "new-file"), join(project, "src", "link-file"));
    try {
      const scoped = policy([
        rule({ capability: "read", tool: "read", pattern: "src/*", action: "allow" }),
        rule({ capability: "read", tool: "read", action: "deny" }),
      ]);
      const writeScoped = policy([
        rule({ capability: "write", tool: "write", pattern: "src/*", action: "allow" }),
        rule({ capability: "write", tool: "write", action: "deny" }),
      ]);

      expect(
        decideToolCall(scoped, "read", { path: "src/link/secret" }, project)?.action,
      ).toBe("deny");
      expect(
        decideToolCall(scoped, "read", { path: "src/link/../secret" }, project)?.action,
      ).toBe("deny");
      expect(
        decideToolCall(writeScoped, "write", { path: "src/link-file" }, project)?.action,
      ).toBe("deny");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("policy loading", () => {
  it("is a no-op only when the policy variable is absent", () => {
    const handlers = new Map<string, (...args: any[]) => any>();
    meridianPermissionsExtension({
      on: (name: string, handler: (...args: any[]) => any) => handlers.set(name, handler),
    } as any);
    expect(handlers).toEqual(new Map());
  });

  it("fails closed when the policy variable is empty", () => {
    process.env._MERIDIAN_PI_PERMISSION_POLICY = "";
    expect(() => meridianPermissionsExtension({ on: () => undefined } as any)).toThrow(
      "Pi permission policy is empty",
    );
  });

  it("fails closed when the policy is malformed JSON", () => {
    process.env._MERIDIAN_PI_PERMISSION_POLICY = "{";
    expect(() => meridianPermissionsExtension({ on: () => undefined } as any)).toThrow();
  });

  it("refuses completion grants outside the fixed command set", () => {
    process.env._MERIDIAN_PI_PERMISSION_POLICY = JSON.stringify(
      policy([], { completion_commands: ["meridian spawn done; printf hidden"] }),
    );
    expect(() => meridianPermissionsExtension({ on: () => undefined } as any)).toThrow(
      "Pi permission policy has an unsupported shape",
    );
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
