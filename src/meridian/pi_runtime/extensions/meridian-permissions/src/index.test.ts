import { afterEach, describe, expect, it } from "vitest";

import meridianPermissionsExtension from "./index.js";

// A stand-in evaluator: prints $FAKE_ANSWER, or exits with $FAKE_EXIT.
const FAKE = [
  "node",
  "-e",
  "process.stdin.resume();process.stdin.on('end',()=>{process.stderr.write('boom');" +
    "if(process.env.FAKE_EXIT)process.exit(Number(process.env.FAKE_EXIT));" +
    "process.stdout.write(process.env.FAKE_ANSWER)})",
  "--",
];

function answer(action: string, reason = "why"): string {
  return JSON.stringify({ action, detail: "echo hi", reason });
}

function load(env: Record<string, string>) {
  Object.assign(process.env, {
    _MERIDIAN_TOOL_POLICY: "{}",
    _MERIDIAN_TOOL_EVALUATOR: JSON.stringify(FAKE),
    ...env,
  });
  const handlers = new Map<string, (...args: any[]) => any>();
  meridianPermissionsExtension({
    on: (name: string, handler: (...args: any[]) => any) => handlers.set(name, handler),
  } as any);
  return (ctx: object = { mode: "rpc", ui: {} }) =>
    handlers.get("tool_call")?.(
      { toolCallId: "call-1", toolName: "bash", input: { command: "echo hi" } },
      ctx,
    );
}

afterEach(() => {
  for (const name of [
    "_MERIDIAN_TOOL_POLICY",
    "_MERIDIAN_TOOL_EVALUATOR",
    "MERIDIAN_SESSION_ROLE",
    "FAKE_ANSWER",
    "FAKE_EXIT",
  ]) {
    delete process.env[name];
  }
});

describe("tool_call", () => {
  it("lets an allowed call run", async () => {
    expect(await load({ FAKE_ANSWER: answer("allow") })()).toBeUndefined();
  });

  it("blocks a denied call with the evaluator's reason", async () => {
    expect(await load({ FAKE_ANSWER: answer("deny", "no") })()).toEqual({ block: true, reason: "no" });
  });

  it("blocks headless ask with the evaluator's reason", async () => {
    const call = load({ FAKE_ANSWER: answer("ask", "nobody to ask"), MERIDIAN_SESSION_ROLE: "spawned" });
    expect(await call({ mode: "rpc", ui: { confirm: async () => true } })).toEqual({
      block: true,
      reason: "nobody to ask",
    });
  });

  it("routes primary ask through Pi's TUI confirmation", async () => {
    const details: string[] = [];
    const call = load({ FAKE_ANSWER: answer("ask"), MERIDIAN_SESSION_ROLE: "primary" });
    const confirm = async (_title: string, detail: string) => (details.push(detail), true);
    expect(await call({ mode: "tui", ui: { confirm } })).toBeUndefined();
    expect(details).toEqual(["echo hi"]);
  });

  it("blocks when the user declines", async () => {
    const call = load({ FAKE_ANSWER: answer("ask"), MERIDIAN_SESSION_ROLE: "primary" });
    expect(await call({ mode: "tui", ui: { confirm: async () => false } })).toEqual({
      block: true,
      reason: "Denied by the user.",
    });
  });

  it.each([
    ["the evaluator crashes", { FAKE_EXIT: "1" }],
    ["it answers with something else", { FAKE_ANSWER: "not json" }],
    ["it answers with an unknown action", { FAKE_ANSWER: answer("maybe") }],
  ])("blocks when %s", async (_name, env) => {
    expect(await load(env)()).toMatchObject({
      block: true,
      reason: expect.stringContaining("could not be evaluated"),
    });
  });

  it("blocks when the evaluator cannot start", async () => {
    const call = load({ _MERIDIAN_TOOL_EVALUATOR: JSON.stringify(["/nonexistent/evaluator"]) });
    expect(await call()).toMatchObject({ block: true, reason: expect.stringContaining("could not be evaluated") });
  });
});

describe("loading", () => {
  it.each([
    ["no policy", { _MERIDIAN_TOOL_POLICY: "" }],
    ["no evaluator", { _MERIDIAN_TOOL_EVALUATOR: "" }],
    ["a malformed evaluator", { _MERIDIAN_TOOL_EVALUATOR: "{" }],
    ["an empty evaluator command", { _MERIDIAN_TOOL_EVALUATOR: "[]" }],
  ])("throws instead of loading with %s", (_name, env) => {
    expect(() => load(env)).toThrow();
  });
});
